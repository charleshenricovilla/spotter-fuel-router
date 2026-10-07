import pandas as pd
import time
from geopy.geocoders import Nominatim
from django.core.management.base import BaseCommand
from api.models import TruckStop

class Command(BaseCommand):
    help = 'Load and geocode fuel stations from CSV'

    def handle(self, *args, **kwargs):
        df = pd.read_csv('fuel-prices-for-be-assessment.csv')
        geolocator = Nominatim(user_agent="fuel_routing_app_unique")
        
        # Cache to avoid duplicate API calls for the same city
        location_cache = {}
        
        total_rows = len(df)
        self.stdout.write(f"Starting to process {total_rows} rows...")

        for index, row in df.iterrows():
            opis_id = row['OPIS Truckstop ID']
            
            # Skip if already exists in DB
            if TruckStop.objects.filter(opis_id=opis_id).exists():
                continue
            
            query = f"{row['City']}, {row['State']}, USA"
            
            # Check cache first
            if query in location_cache:
                lat, lon = location_cache[query]
            else:
                try:
                    location = geolocator.geocode(query, timeout=10)
                    lat, lon = (location.latitude, location.longitude) if location else (None, None)
                    location_cache[query] = (lat, lon)
                    time.sleep(1)  # Only sleep on actual API calls
                except Exception:
                    lat, lon = None, None
                    time.sleep(1) # Sleep on error to prevent spamming
                
            TruckStop.objects.create(
                opis_id=opis_id,
                name=row['Truckstop Name'],
                address=row['Address'],
                city=row['City'],
                state=row['State'],
                rack_id=row['Rack ID'],
                retail_price=row['Retail Price'],
                latitude=lat,
                longitude=lon
            )
            
            # Print progress every 100 rows
            if (index + 1) % 100 == 0:
                self.stdout.write(f"Processed {index + 1}/{total_rows} rows...")
                
        self.stdout.write(self.style.SUCCESS('Successfully loaded truck stops'))