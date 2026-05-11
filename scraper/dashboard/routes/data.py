from fastapi import APIRouter, Request, HTTPException
from bson import ObjectId
try:
    from ..db import db, CATEGORY_MAP
except ImportError:
    from db import db, CATEGORY_MAP

router = APIRouter(prefix="/api")

@router.get("/data/{category}/{vehicle_id}")
async def get_category_data(category: str, vehicle_id: str):
    col_name = CATEGORY_MAP.get(category)
    if not col_name:
        raise HTTPException(status_code=400, detail="Invalid category")
    
    data = await db[col_name].find({"vehicle_id": ObjectId(vehicle_id)}).to_list(1000)
    for item in data:
        item["_id"] = str(item["_id"])
        item["vehicle_id"] = str(item["vehicle_id"])
    return data

@router.post("/data/{category}/{vehicle_id}")
async def create_record(category: str, vehicle_id: str, request: Request):
    col_name = CATEGORY_MAP.get(category)
    if not col_name:
        raise HTTPException(status_code=400, detail="Invalid category")
    
    data = await request.json()
    data["vehicle_id"] = ObjectId(vehicle_id)
    
    result = await db[col_name].insert_one(data)
    return {"id": str(result.inserted_id), "message": "Record created"}

@router.put("/data/{category}/{record_id}")
async def update_record(category: str, record_id: str, request: Request):
    col_name = CATEGORY_MAP.get(category)
    if not col_name:
        raise HTTPException(status_code=400, detail="Invalid category")
    
    data = await request.json()
    if "_id" in data: del data["_id"]
    if "vehicle_id" in data: data["vehicle_id"] = ObjectId(data["vehicle_id"])
    
    result = await db[col_name].update_one({"_id": ObjectId(record_id)}, {"$set": data})
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Record not found")
    return {"message": "Record updated"}

@router.delete("/data/{category}/{record_id}")
async def delete_record(category: str, record_id: str):
    col_name = CATEGORY_MAP.get(category)
    if not col_name:
        raise HTTPException(status_code=400, detail="Invalid category")

    result = await db[col_name].delete_one({"_id": ObjectId(record_id)})
    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Record not found")
    return {"message": "Record deleted"}

# ─── Manual / Service Manual ─────────────────────────────────────────────────
@router.get("/manual/{vehicle_id}")
async def get_manual(vehicle_id: str):
    manuals = await db.manuals.find({"vehicle_id": ObjectId(vehicle_id)}).to_list(1000)
    for m in manuals:
        m["_id"] = str(m["_id"])
        m["vehicle_id"] = str(m["vehicle_id"])
    return manuals
