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
from django.contrib.gis.geos import Point, LineString
from django.contrib.gis.db.models.functions import Distance
from django.contrib.gis.measure import D
from routing.models import FuelStation
from math import ceil
import hashlib

ORS_API_KEY = os.getenv('ORS_API_KEY')
CLIENT = openrouteservice.Client(key=ORS_API_KEY)
logger = logging.getLogger(__name__)
MAX_RANGE_MILES = 500
MPG = 10
SEARCH_RADIUS_MILES = 25
GALLONS_PER_REFUEL = MAX_RANGE_MILES / MPG
MILES_TO_DEGREES = 1/69
SRID = 4326


def calculate_route_segments(route_coordinates, max_segment_length=MAX_RANGE_MILES):
    """
    Split route into segments where each segment is approximately max_segment_length miles long.
    Returns list of segments with actual distances calculated.
    """
    segments = []
    current_segment = [route_coordinates[0]]
    cumulative_distance = 0
    segment_distances = []

    for i in range(1, len(route_coordinates)):
        prev_point = (route_coordinates[i - 1][1], route_coordinates[i - 1][0])
        current_point = (route_coordinates[i][1], route_coordinates[i][0])
        dist = geodesic(prev_point, current_point).miles
        
        # If adding this segment would exceed max length, finalize current segment
        if cumulative_distance + dist > max_segment_length:
            # Find optimal split point near max_segment_length
            remaining_dist = max_segment_length - cumulative_distance
            ratio = remaining_dist / dist
            split_point = [
                route_coordinates[i-1][0] + ratio * (route_coordinates[i][0] - route_coordinates[i-1][0]),
                route_coordinates[i-1][1] + ratio * (route_coordinates[i][1] - route_coordinates[i-1][1])
            ]
            current_segment.append(split_point)
            segments.append(current_segment)
            segment_distances.append(cumulative_distance + remaining_dist)
            
            # Start new segment with the remaining part
            current_segment = [split_point, route_coordinates[i]]
            cumulative_distance = dist - remaining_dist
        else:
            current_segment.append(route_coordinates[i])
            cumulative_distance += dist

    if len(current_segment) > 1:
        segments.append(current_segment)
        segment_distances.append(cumulative_distance)

    return segments, segment_distances


def find_optimal_fuel_stops(route_segment, segment_distance):
    """
    Find optimal fuel stops along a route segment considering:
    - Stations within SEARCH_RADIUS_MILES of any point in the segment
    - Cheapest fuel prices
    - Potentially multiple stops if segment is long
    """
    try:
        # Convert segment to LineString with SRID
        line = LineString([(lon, lat) for lon, lat in route_segment], srid=SRID)

        # Create a buffer around the line
        search_area = line.buffer(SEARCH_RADIUS_MILES * MILES_TO_DEGREES)

        # Cache all stations in the buffered area ordered by price
        stations = list(FuelStation.objects.filter(
            location__intersects=search_area
        ).order_by('price'))

        if not stations:
            return None

        # Determine if we need multiple stops for this segment
        required_stops = max(1, ceil(segment_distance / MAX_RANGE_MILES))
        optimal_stops = []

        for i in range(required_stops):
            # Determine target point along route
            segment_ratio = (i + 0.5) / required_stops
            segment_index = min(int(len(route_segment) * segment_ratio), len(route_segment) - 1)
            stop_point = route_segment[segment_index]

            # Create Point with SRID
            point = Point(stop_point[0], stop_point[1], srid=SRID)

            # Filter stations near this point within SEARCH_RADIUS_MILES
            nearby = sorted(
                [s for s in stations if s.location.distance(point) <= D(mi=SEARCH_RADIUS_MILES).m],
                key=lambda s: (s.price, s.location.distance(point))
            )

            if nearby:
                nearest_cheapest = nearby[0]
                gallons = min(segment_distance / required_stops, MAX_RANGE_MILES) / MPG
                cost = round(nearest_cheapest.price * gallons, 2)

                loc = nearest_cheapest.location
                longitude = loc.x if hasattr(loc, 'x') else None
                latitude = loc.y if hasattr(loc, 'y') else None

                optimal_stops.append({
                    "location": nearest_cheapest.name,
                    "address": nearest_cheapest.address,
                    "city": nearest_cheapest.city,
                    "state": nearest_cheapest.state,
                    "price": nearest_cheapest.price,
                    "latitude": latitude,
                    "longitude": longitude,
                    "gallons": round(gallons, 2),
                    "cost": cost,
                    "segment_distance": round(segment_distance / required_stops, 2)
                })

        return optimal_stops if optimal_stops else None

    except Exception as e:
        logger.error(f"Error finding fuel stops: {str(e)}")
        return None


@method_decorator(csrf_exempt, name='dispatch')
class OptimizedRouteView(View):
    def post(self, request):
        try:
            data = json.loads(request.body)
            start = data.get('start')
            end = data.get('end')
            
            if not start or not end:
                return JsonResponse({"error": "start and end are required"}, status=400)

            cache_key = self.generate_cache_key(start, end)
            cached = cache.get(cache_key)
            if cached:
                return JsonResponse(cached)

            start_coords = self.get_coordinates(start)
            end_coords = self.get_coordinates(end)

            if not start_coords or not end_coords:
                return JsonResponse({"error": "Unable to geocode start or end location."}, status=400)
            
            if not self.verify_us_location(start_coords) or not self.verify_us_location(end_coords):
                return JsonResponse({"error": "Both locations must be within the USA"}, status=400)

            route = CLIENT.directions(
                coordinates=[start_coords, end_coords],
                profile='driving-car',
                format='geojson'
            )

            route_coords = route['features'][0]['geometry']['coordinates']
            segments, segment_distances = calculate_route_segments(route_coords)

            fuel_stops = []
            for segment, distance in zip(segments, segment_distances):
                stops = find_optimal_fuel_stops(segment, distance)
                if stops:
                    fuel_stops.extend(stops)

            if not fuel_stops:
                return JsonResponse({"error": "No fuel stations found along the route"}, status=404)

            total_fuel_cost = sum(stop["cost"] for stop in fuel_stops)

            waypoints = [start_coords]
            for stop in fuel_stops:
                waypoints.append((stop['longitude'], stop['latitude']))
            waypoints.append(end_coords)

            a_param = ",".join(f"{lat},{lon}" for lon, lat in waypoints)
            map_url = f"https://maps.openrouteservice.org/directions?n1={start_coords[1]}&n2={start_coords[0]}&a={a_param}&b=0&c=0&k1=en-US&k2=mi"

            response_data = {
                "route_map_url": map_url,
                "total_fuel_cost": round(total_fuel_cost, 2),
            }

            cache.set(cache_key, response_data, timeout=3600)
            return JsonResponse(response_data)

        except Exception as e:
            logger.exception("Error computing optimized route")
            return JsonResponse({"error": str(e)}, status=500)

    def get_coordinates(self, location):
        """Get coordinates for a location string using Nominatim"""
        url = f"https://nominatim.openstreetmap.org/search?q={location}&format=json&limit=1"
        headers = {'User-Agent': 'fuel-route-app/1.0'}
        try:
            res = requests.get(url, headers=headers)
            if res.status_code == 200:
                data = res.json()
                if data:
                    return float(data[0]['lon']), float(data[0]['lat'])
            return None
        except (requests.RequestException, ValueError, KeyError, IndexError) as e:
            logger.error(f"Error getting coordinates for {location}: {e}")
            return None

    def verify_us_location(self, coords):
        """Verify coordinates are within USA bounds"""
        min_lon, max_lon = -125.0, -66.0
        min_lat, max_lat = 24.0, 50.0
        lon, lat = coords
        return min_lon <= lon <= max_lon and min_lat <= lat <= max_lat
    
    def generate_cache_key(start, end):
        raw_key = f"{start}|{end}"
        return f"route:{hashlib.md5(raw_key.encode()).hexdigest()}"