import csv
import requests
import time
import re
from django.core.management.base import BaseCommand
from django.contrib.gis.geos import Point
from routing.models import FuelStation

class Command(BaseCommand):
    help = 'Load fuel stations from CSV and save to PostgreSQL/PostGIS'

    def handle(self, *args, **kwargs):
        file_path = './fuel-prices.csv'
        fuel_stations_to_create = []
        geocoding_cache = {}

        def clean_address(address):
            cleaned = re.sub(r'EXIT\s*\d+', '', address, flags=re.IGNORECASE)
            cleaned = re.sub(r'[&]', 'and', cleaned)
            cleaned = re.sub(r'\s{2,}', ' ', cleaned)
            cleaned = cleaned.strip(' ,')
            return cleaned

        def geocode_address(full_address, fallback=None):
            if full_address in geocoding_cache:
                return geocoding_cache[full_address]

            geocode_url = f"https://nominatim.openstreetmap.org/search?q={full_address}&format=json&addressdetails=1&limit=1"
            try:
                response = requests.get(geocode_url, timeout=5, headers={"User-Agent": "fuel-locator-app"})
                if response.status_code == 200 and response.text.strip():
                    data = response.json()
                    if data:
                        lat, lon = float(data[0]['lat']), float(data[0]['lon'])
                        geocoding_cache[full_address] = (lat, lon)
                        return lat, lon
            except Exception as e:
                self.stderr.write(f"Geocode error for '{full_address}': {e}")

            if fallback:
                time.sleep(0.5)
                return geocode_address(fallback)
            return None, None

        with open(file_path, mode='r') as file:
            reader = csv.DictReader(file)
            for i, row in enumerate(reader):
                name = row['Truckstop Name']
                address = row['Address']
                city = row['City']
                state = row['State']

                try:
                    price = float(row['Retail Price'])
                except ValueError:
                    self.stderr.write(f"[{i+1}] Invalid price, skipping.")
                    continue

                cleaned_address = clean_address(address)
                full_address = f"{cleaned_address}, {city}, {state}"
                fallback_address = f"{city}, {state}"

                lat, lon = geocode_address(full_address, fallback=fallback_address)

                if lat is None or lon is None:
                    self.stdout.write(f"[{i+1}] Geocoding failed: {name} - {address}, {city}, {state}")
                    continue

                location = Point(lon, lat)

                fuel_station = FuelStation(
                    name=name,
                    address=address,
                    city=city,
                    state=state,
                    price=price,
                    location=location
                )
                fuel_stations_to_create.append(fuel_station)

                self.stdout.write(f"[{i+1}] Queued: {name} - {city}, {state}")

                if len(fuel_stations_to_create) >= 100:
                    FuelStation.objects.bulk_create(fuel_stations_to_create)
                    fuel_stations_to_create.clear()
                    self.stdout.write(f"[{i+1}] Batch inserted, continuing...")

                time.sleep(0.2)

            if fuel_stations_to_create:
                FuelStation.objects.bulk_create(fuel_stations_to_create)

        self.stdout.write("All fuel stations have been processed and saved.")
