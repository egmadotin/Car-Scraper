import argparse
import subprocess
import sys
import os

def main():
    parser = argparse.ArgumentParser(description="ProDemand Hierarchical Vehicle Scraper")
    parser.add_argument("--year", help="Specific year to scrape all vehicles for")
    parser.add_argument("--force", action="store_true", help="Force re-extraction of already scraped vehicles")
    args = parser.parse_args()

    print(f"Starting ProDemand Hierarchical Vehicle Scraper{' for ' + args.year if args.year else ''}...")
    scraper_path = os.path.join("vehicle_hierarchy", "vehicle_selector.py")
    
    if not os.path.exists(scraper_path):
        print(f"Error: Scraper not found at {scraper_path}")
        return

    try:
        # Run the new scraper with the year argument if provided
        # Use only the filename since we set cwd to the directory
        cmd = [sys.executable, os.path.basename(scraper_path)]
        if args.year:
            cmd.extend(["--year", args.year])
        if args.force:
            cmd.append("--force")
            
        subprocess.run(cmd, check=True, cwd=os.path.dirname(os.path.abspath(scraper_path)))
    except KeyboardInterrupt:
        print("\nScraper stopped by user.")
    except Exception as e:
        print(f"Error running scraper: {e}")

if __name__ == "__main__":
    main()
