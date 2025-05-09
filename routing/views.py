import os
import json
import logging
import requests
import openrouteservice
from django.http import JsonResponse
from django.views import View
from django.core.cache import cache
from django.views.decorators.csrf import csrf_exempt
from django.utils.decorators import method_decorator
from geopy.distance import geodesic
from django.contrib.gis.geos import Point
from django.contrib.gis.db.models.functions import Distance
from routing.models import FuelStation

ORS_API_KEY = os.getenv('ORS_API_KEY')
CLIENT = openrouteservice.Client(key=ORS_API_KEY)
logger = logging.getLogger(__name__)


def segment_route(route_coordinates, segment_distance=500):
    segments = []
    current_segment = [route_coordinates[0]]
    cumulative_distance = 0

    for i in range(1, len(route_coordinates)):
        prev_point = (route_coordinates[i - 1][1], route_coordinates[i - 1][0])
        current_point = (route_coordinates[i][1], route_coordinates[i][0])

        dist = geodesic(prev_point, current_point).miles
        cumulative_distance += dist
        current_segment.append(route_coordinates[i])

        if cumulative_distance >= segment_distance:
            segments.append(current_segment)
            current_segment = [route_coordinates[i]]
            cumulative_distance = 0

    if len(current_segment) > 1:
        segments.append(current_segment)

    return segments


@method_decorator(csrf_exempt, name='dispatch')
class OptimizedRouteView(View):
    def post(self, request):
        try:
            data = json.loads(request.body)
            start = data.get('start')
            end = data.get('end')
            if not start or not end:
                return JsonResponse({"error": "start and end are required"}, status=400)

            cache_key = f"route:{start}:{end}"
            cached = cache.get(cache_key)
            if cached:
                return JsonResponse(cached)

            start_coords = self.get_coordinates(start)
            end_coords = self.get_coordinates(end)

            if not start_coords or not end_coords:
                return JsonResponse({"error": "Unable to geocode start or end location."}, status=400)

            route = CLIENT.directions(
                coordinates=[start_coords, end_coords],
                profile='driving-car',
                format='geojson'
            )

            route_coords = route['features'][0]['geometry']['coordinates']
            segments = segment_route(route_coords)

            fuel_stops = []
            for segment in segments:
                stop = self.get_cheapest_fuel_stop_postgis(segment)
                if stop:
                    gallons = 500 / 10
                    cost = round(stop['price'] * gallons, 2)
                    stop.update({"gallons": gallons, "cost": cost})
                    fuel_stops.append(stop)

            total_fuel_cost = sum(stop["cost"] for stop in fuel_stops)

            waypoints = [start_coords]
            for stop in fuel_stops:
                waypoints.append((stop['longitude'], stop['latitude']))
            waypoints.append(end_coords)

            a_param = ",".join(f"{lat},{lon}" for lon, lat in waypoints)
            map_url = f"https://maps.openrouteservice.org/directions?n1={start_coords[1]}&n2={start_coords[0]}&a={a_param}&b=0&c=0&k1=en-US&k2=mi"

            response_data = {
                "route_map_url": map_url,
                "fuel_stops": fuel_stops,
                "total_fuel_cost": round(total_fuel_cost, 2)
            }

            cache.set(cache_key, response_data, timeout=3600)
            return JsonResponse(response_data)

        except Exception as e:
            logger.exception("Error computing optimized route")
            return JsonResponse({"error": str(e)}, status=500)

    def get_coordinates(self, location):
        url = f"https://nominatim.openstreetmap.org/search?q={location}&format=json&limit=1"
        headers = {'User-Agent': 'fuel-route-app/1.0'}
        try:
            res = requests.get(url, headers=headers)
            if res.status_code == 200:
                data = res.json()
                if data:
                    country = None
                    if 'address' in data[0]:
                        country = data[0]['address'].get('country')
                    if not country and 'display_name' in data[0]:
                        display_parts = data[0]['display_name'].split(',')
                        if display_parts:
                            country = display_parts[-1].strip()

                    if country and country.lower() in ['united states', 'usa']:
                        return float(data[0]['lon']), float(data[0]['lat'])
            return None
        except (requests.RequestException, ValueError, KeyError, IndexError) as e:
            print(f"Error getting coordinates for {location}: {e}")
            return None



    def get_cheapest_fuel_stop_postgis(self, segment):
        lat_lon_points = [(lat, lon) for lon, lat in segment]
        avg_lat = sum(lat for lat, _ in lat_lon_points) / len(lat_lon_points)
        avg_lon = sum(lon for _, lon in lat_lon_points) / len(lat_lon_points)
        center_point = Point(avg_lon, avg_lat, srid=4326)

        stations = FuelStation.objects.annotate(
            distance=Distance('location', center_point)
        ).filter(
            distance__lte=16093.4
        ).order_by('price')

        if stations.exists():
            s = stations.first()
            return {
                "location": s.name,
                "address": s.address,
                "city": s.city,
                "state": s.state,
                "price": s.price,
                "latitude": s.location.y,
                "longitude": s.location.x,
            }

        return None
