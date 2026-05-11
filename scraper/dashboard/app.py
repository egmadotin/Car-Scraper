from fastapi import FastAPI, Request
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
import os
import uvicorn
from .db import db
from .routes import vehicles, data, upload

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

app = FastAPI(title="Vehicle Hierarchy Explorer")
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))

# Include modular routes
app.include_router(vehicles.router)
app.include_router(data.router)
app.include_router(upload.router)

@app.get("/")
async def home(request: Request):
    # Fetch all unique years from the master collection
    years_doc = await db.years.find_one({})
    years = years_doc.get("values", []) if years_doc else []
    return templates.TemplateResponse("index.html", {"request": request, "years": sorted(years, reverse=True)})

@app.get("/vehicle/{vehicle_id}")
async def vehicle_detail(request: Request, vehicle_id: str):
    from bson import ObjectId
    v = await db.vehicles.find_one({"_id": ObjectId(vehicle_id)})
    if not v:
        return "Vehicle not found", 404
    v["_id"] = str(v["_id"])
    return templates.TemplateResponse("vehicle.html", {"request": request, "vehicle": v})

if __name__ == "__main__":
    uvicorn.run("dashboard.app:app", host="0.0.0.0", port=8081, reload=True)
