from pymongo import MongoClient
import os
from dotenv import load_dotenv

load_dotenv("d:/FREELANCING/CHINA/Car Chat/scraper/.env")
MONGO_URI = os.getenv("MONGODB_URI", "mongodb://localhost:27017")
client = MongoClient(MONGO_URI)

print(f"Connecting to: {MONGO_URI}")
dbs = client.list_database_names()
print(f"Databases: {dbs}")

for db_name in dbs:
    if db_name in ['admin', 'config', 'local']: continue
    db = client[db_name]
    print(f"\nDatabase: {db_name}")
    for col in db.list_collection_names():
        count = db[col].count_documents({})
        if count > 0:
            print(f"  Collection: {col}, Count: {count}")
