"""
End to end encryption layer for Telegram
"""

import os
import sys
import logging
import asyncio
import base64


from secrets import token_bytes
from getpass import getpass

# Crypto
from cryptography.hazmat.primitives import hashes, serialization, padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.backends import default_backend

import requests

# Telethon
from telethon import TelegramClient, events
from telethon.errors import SessionPasswordNeededError
from telethon.network import ConnectionTcpAbridged
from telethon.utils import get_display_name

from utils import print_title, get_public_key, get_env, sprint, BUCKET_URL

from db import Dialog, BLOBS_DIR


logging.basicConfig(
    format="[%(levelname) 5s/%(asctime)s] %(name)s: %(message)s", level=logging.INFO
)


# Create a global variable to hold the loop we will be using
loop = asyncio.get_event_loop()


async def async_input(prompt):
    """
    Python's ``input()`` is blocking, which means the event loop we set
    above can't be running while we're blocking there. This method will
    let the loop run while we wait for input.
    """
    print(prompt, end="", flush=True)
    return (await loop.run_in_executor(None, sys.stdin.readline)).rstrip()


class InteractiveTelegramClient(TelegramClient):
    def __init__(self, session_user_id, api_id, api_hash, proxy=None):

        print_title("Initialization")

        super().__init__(
            session_user_id,
            api_id,
            api_hash,
            connection=ConnectionTcpAbridged,
            proxy=proxy,
        )

        print("Connecting to Telegram servers...")
        try:
            loop.run_until_complete(self.connect())
        except IOError:
            # We handle IOError and not ConnectionError because
            # PySocks' errors do not subclass ConnectionError
            # (so this will work with and without proxies).
            print("Initial connection failed. Retrying...")
            loop.run_until_complete(self.connect())

        if not loop.run_until_complete(self.is_user_authorized()):
            print("First run. Sending code request...")
            user_phone = input("Enter your phone: ")
            loop.run_until_complete(self.sign_in(user_phone))

            self_user = None
            while self_user is None:
                code = input("Enter the code you just received: ")
                try:
                    self_user = loop.run_until_complete(self.sign_in(code=code))

                except SessionPasswordNeededError:
                    pw = getpass(
                        "Two step verification is enabled. "
                        "Please enter your password: "
                    )

                    self_user = loop.run_until_complete(self.sign_in(password=pw))

    async def run(self):
        """Main loop of the TelegramClient, will wait for user action"""

        self.add_event_handler(self.message_handler, events.NewMessage(incoming=True))

        # Enter a while loop to chat as long as the user wants
        while True:
            dialog_count = 15

            dialogs = await self.get_dialogs(limit=dialog_count)

            i = None
            while i is None:
                print_title("Dialogs window")

                # Display them so the user can choose
                for i, dialog in enumerate(dialogs, start=1):
                    sprint("{}. {}".format(i, get_display_name(dialog.entity)))

                # Let the user decide who they want to talk to
                print()
                print("> Who do you want to send messages to?")
                print("> Available commands:")
                print("  !q: Quits the dialogs window and exits.")
                print("  !l: Logs out, terminating this session.")
                print()
                i = await async_input("Enter dialog ID or a command: ")
                if i == "!q":
                    return
                if i == "!l":
                    await self.log_out()
                    return

                try:
                    i = int(i if i else 0) - 1
                    # Ensure it is inside the bounds, otherwise retry
                    if not 0 <= i < dialog_count:
                        i = None
                except ValueError:
                    i = None

            # Retrieve the selected user (or chat, or channel)
            entity = dialogs[i].entity

            # Show some information
            print_title('Chat with "{}"'.format(get_display_name(entity)))
            print("Available commands:")
            print("  !q:  Quits the current chat.")
            print("  !Q:  Quits the current chat and exits.")

            print()

            # And start a while loop to chat
            while True:
                msg = await async_input("Enter a message: ")
                # Quit
                if msg == "!q":
                    break
                if msg == "!Q":
                    return

                # Send chat message (if any)
                if msg:
                    # If the receiver's aes key is not present,
                    # fetch his public key from server and derive a aes key

                    print("SENDING MESSAGE TO ENTITTY: ", entity.id)
                    aes_shared_key = None
                    for dlg in Dialog.select():
                        if dlg.dialog_id == entity.id:
                            # found a entry of aes shared key.
                            aes_shared_key = dlg.aes_shared_key
                            break

                    if aes_shared_key is None:
                        # get the public key.
                        peer_pub_key = get_public_key(entity.id)
                        shared_key = my_ecdh_private_key.exchange(
                            ec.ECDH(), peer_pub_key
                        )
                        aes_shared_key = HKDF(
                            algorithm=hashes.SHA256(),
                            length=32,
                            salt=None,
                            info=None,
                            backend=default_backend(),
                        ).derive(shared_key)
                        peer = Dialog(
                            dialog_id=entity.id, aes_shared_key=aes_shared_key
                        )
                        peer.save(force_insert=True)

                    init_vector = token_bytes(16)
                    aes = Cipher(
                        algorithms.AES(aes_shared_key),
                        modes.CBC(init_vector),
                        backend=default_backend(),
                    )
                    encryptor = aes.encryptor()

                    padder = padding.PKCS7(128).padder()
                    padded_data = padder.update(msg.encode("utf-8")) + padder.finalize()
                    enc_msg_bytes = encryptor.update(padded_data) + encryptor.finalize()
                    enc_msg_bytes = init_vector + enc_msg_bytes
                    b64_enc_txt = base64.b64encode(enc_msg_bytes).decode("utf-8")
                    await self.send_message(entity, b64_enc_txt, link_preview=False)

    async def message_handler(self, event):
        """Callback method for received events.NewMessage"""

        if event.text:
            # check if the required aes key is present.
            aes_shared_key = None
            for dlg in Dialog.select():
                if dlg.dialog_id == event.sender_id:
                    # found a entry of aes key shared with receiver.
                    aes_shared_key = dlg.aes_shared_key
                    break

            if aes_shared_key is None:
                # get the public key.
                peer_pub_key = get_public_key(event.sender_id)
                shared_key = my_ecdh_private_key.exchange(ec.ECDH(), peer_pub_key)
                aes_shared_key = HKDF(
                    algorithm=hashes.SHA256(),
                    length=32,
                    salt=None,
                    info=None,
                    backend=default_backend(),
                ).derive(shared_key)

                peer = Dialog(dialog_id=event.sender_id, aes_shared_key=aes_shared_key)
                peer.save(force_insert=True)

            # Decrypt the msg and print.
            b64_enc_text_bytes = event.text.encode("utf-8")
            encr_msg_bytes = base64.b64decode(b64_enc_text_bytes)
            init_vector = encr_msg_bytes[:16]
            aes = Cipher(
                algorithms.AES(aes_shared_key),
                modes.CBC(init_vector),
                backend=default_backend(),
            )
            decryptor = aes.decryptor()

            temp_bytes = decryptor.update(encr_msg_bytes[16:]) + decryptor.finalize()

            unpadder = padding.PKCS7(128).unpadder()
            temp_bytes = unpadder.update(temp_bytes) + unpadder.finalize()
            event.text = temp_bytes.decode("utf-8")

            chat = await event.get_chat()
            if event.is_group:
                sprint(
                    '<< {} @ {} sent "{}"'.format(
                        get_display_name(await event.get_sender()),
                        get_display_name(chat),
                        event.text,
                    )
                )
            else:
                sprint('<< {} sent "{}"'.format(get_display_name(chat), event.text))


async def get_my_id(client):
    me = await client.get_me()
    return me.id


if __name__ == "__main__":
    SESSION = os.environ.get("TG_SESSION", "interactive")
    API_ID = get_env("TG_API_ID", "Enter your API ID: ", int)
    API_HASH = get_env("TG_API_HASH", "Enter your API hash: ")

    try:
        with open(BLOBS_DIR + "my_ecdh_private_key.pem", "rb") as f:
            my_ecdh_private_key = serialization.load_pem_private_key(
                f.read(), password=None
            )
        with open(BLOBS_DIR + "my_ecdh_public_key.pem", "rb") as f:
            serialized_public_key = f.read()
            my_ecdh_public_key = serialization.load_pem_public_key(
                serialized_public_key
            )
    except FileNotFoundError:

        my_ecdh_private_key = ec.generate_private_key(ec.SECP384R1())
        my_ecdh_public_key = my_ecdh_private_key.public_key()

        serialized_private_key = my_ecdh_private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
        serialized_public_key = my_ecdh_public_key.public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        with open(BLOBS_DIR + "my_ecdh_private_key.pem", "wb") as f:
            f.write(serialized_private_key)
        with open(BLOBS_DIR + "my_ecdh_public_key.pem", "wb") as f:
            f.write(serialized_public_key)

    client = InteractiveTelegramClient(SESSION, API_ID, API_HASH)

    my_entity_id = str(loop.run_until_complete(get_my_id(client)))

    r = requests.get(url=BUCKET_URL + my_entity_id)
    if r.status_code == 404:
        print("Uploading public key to server!!")
        data = {"pub_key": base64.b64encode(serialized_public_key).decode("utf-8")}
        requests.post(url=BUCKET_URL + "update/" + my_entity_id, data=data)

    loop.run_until_complete(client.run())
