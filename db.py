import os
import sys


# Database ORM
from peewee import IntegerField, BlobField, SqliteDatabase, Model

BLOBS_DIR = "blobs/"

if not os.path.exists(BLOBS_DIR):
    os.makedirs(BLOBS_DIR)

db_file = str(sys.argv[1]) if len(sys.argv) > 1 else "key_store.db"

db = SqliteDatabase("blobs/" + db_file)


class BaseModel(Model):
    class Meta:
        database = db


class Dialog(BaseModel):
    dialog_id = IntegerField(primary_key=True)
    aes_shared_key = BlobField()


db.create_tables([Dialog])
