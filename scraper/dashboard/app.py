from fastapi import FastAPI, Request, HTTPException
from fastapi.templating import Jinja2Templates
from motor.motor_asyncio import AsyncIOMotorClient
import os
import subprocess
import sys
from dotenv import load_dotenv

load_dotenv()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
app = FastAPI(title="Vehicle Hierarchy Explorer")
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))

# MongoDB Connection
MONGO_URI = os.getenv("MONGODB_URI", "mongodb://localhost:27017/")
db_client = AsyncIOMotorClient(MONGO_URI)
db = db_client[os.getenv("DATABASE_NAME", "prodemand_db")]

@app.get("/")
async def index(request: Request):
    # Fetch all unique years
    years = await db.vehicles.distinct("year")
    # Sort years descending
    years = sorted(years, reverse=True)
    return templates.TemplateResponse(request, "index.html", {"years": years})

@app.get("/api/makes/{year}")
async def get_makes(year: str):
    makes = await db.vehicles.distinct("make", {"year": year})
    return sorted(makes)

@app.get("/api/models/{year}/{make}")
async def get_models(year: str, make: str):
    models = await db.vehicles.distinct("model", {"year": year, "make": make})
    return sorted(models)

@app.get("/api/engines/{year}/{make}/{model}")
async def get_engines(year: str, make: str, model: str):
    engines = await db.vehicles.distinct("engine", {"year": year, "make": make, "model": model})
    return sorted(engines)

@app.get("/api/submodels/{year}/{make}/{model}/{engine}")
async def get_submodels(year: str, make: str, model: str, engine: str):
    vehicles = await db.vehicles.find({"year": year, "make": make, "model": model, "engine": engine}).to_list(100)
    # Return list of submodels with their IDs for detail viewing
    return [{"submodel": v["submodel"], "id": str(v["_id"])} for v in vehicles]

@app.get("/vehicle/{id}")
async def vehicle_detail(request: Request, id: str):
    from bson import ObjectId
    vehicle = await db.vehicles.find_one({"_id": ObjectId(id)})
    if not vehicle:
        raise HTTPException(status_code=404, detail="Vehicle not found")
    return templates.TemplateResponse(request, "vehicle.html", {"vehicle": vehicle})

@app.post("/api/scrape")
async def start_scrape(request: Request):
    data = await request.json()
    year = data.get("year")
    
    # Run the new hierarchical scraper
    scraper_path = os.path.join(os.path.dirname(BASE_DIR), "main.py")
    cmd = [sys.executable, scraper_path]
    if year:
        cmd.extend(["--year", str(year)])
    
    try:
        subprocess.Popen(cmd, cwd=os.path.dirname(scraper_path))
        return {"status": "success", "message": f"Hierarchical extraction started for {year if year else 'all years'}"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8081)

