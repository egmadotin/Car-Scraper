import os
from pymongo import MongoClient
from dotenv import load_dotenv

load_dotenv()

MONGO_URI = os.getenv("MONGODB_URI", "mongodb://localhost:27017/")
DB_NAME = "prodemand_db"

client = MongoClient(MONGO_URI)
db = client[DB_NAME]

print("\n--- MongoDB Stats ---")
print(f"Database: {DB_NAME}")
print(f"Years Captured: {db.years.count_documents({})}")
print(f"Vehicles (with Options) Captured: {db.vehicles.count_documents({})}")

print("\n--- Recent Vehicles ---")
for v in db.vehicles.find().sort("timestamp", -1).limit(5):
    print(f"{v['year']} {v['make']} {v['model']} - Options: {list(v['options'].keys())}")

client.close()
