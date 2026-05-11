import os
from pymongo import MongoClient
from dotenv import load_dotenv
import json

load_dotenv()

MONGO_URI = os.getenv("MONGODB_URI", "mongodb://localhost:27017/")
DB_NAME = "prodemand_db"

client = MongoClient(MONGO_URI)
db = client[DB_NAME]

print("--- Last 5 Records ---")
for doc in db.vehicles.find().sort("timestamp", -1).limit(5):
    # Convert ObjectId to string for printing
    doc['_id'] = str(doc['_id'])
    print(json.dumps(doc, indent=2))

client.close()
