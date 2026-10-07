import math
import requests
import folium
from typing import List, Tuple, Dict, Any

from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from geopy.geocoders import Nominatim

from .models import TruckStop

class OptimalRouteAPIView(APIView):
    """
    Handles the core routing and fuel optimization logic.
    We minimize API calls by hitting the free OSRM map API exactly once 
    to grab the route geometry, then process all spatial proximity and 
    fuel optimization locally against our Postgres/SQLite database.
    """

    def get(self, request) -> Response:
        start_location = request.query_params.get('start')
        finish_location = request.query_params.get('finish')

        if not start_location or not finish_location:
            return Response(
                {"error": "Please provide both 'start' and 'finish' query parameters (e.g., ?start=Dallas,TX&finish=Austin,TX)."},
                status=status.HTTP_400_BAD_REQUEST
            )

        # 1. Geocode Coordinates
        coords = self._geocode_locations(start_location, finish_location)
        if not coords:
            return Response(
                {"error": "Could not accurately geocode one or both of the provided locations."},
                status=status.HTTP_422_UNPROCESSABLE_ENTITY
            )
        start_coords, finish_coords = coords

        # 2. Fetch Route Geometry (One single API call)
        route_data = self._fetch_osrm_route(start_coords, finish_coords)
        if not route_data:
            return Response(
                {"error": "Routing service failed to find a valid driving route."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE
            )

        total_distance_miles = route_data['distance'] * 0.000621371
        route_points = [(pt[1], pt[0]) for pt in route_data['geometry']['coordinates']]

        # 3. Find Stations Along the Route
        candidate_stops = self._find_stations_near_route(route_points)

        # 4. Run the Fuel Optimization Algorithm
        optimal_plan, total_cost, is_possible = self._optimize_fuel_stops(candidate_stops, total_distance_miles)

        if not is_possible:
            return Response(
                {"error": "This route is impossible with a 500-mile range due to a lack of gas stations along the way."},
                status=status.HTTP_422_UNPROCESSABLE_ENTITY
            )

        # 5. Generate Visual Map
        map_html = self._generate_folium_map(start_coords, finish_coords, route_data['geometry'], optimal_plan)

        return Response({
            "route_summary": {
                "start": start_location,
                "finish": finish_location,
                "total_distance_miles": round(total_distance_miles, 2),
                "total_fuel_cost_usd": round(total_cost, 2),
                "total_stops_required": len(optimal_plan)
            },
            "fuel_stops": optimal_plan,
            "map_html": map_html 
        })

    def _geocode_locations(self, start: str, finish: str) -> tuple | None:
        """Translates human-readable city/states into lat/lon coordinates."""
        geolocator = Nominatim(user_agent="spotter_fuel_router_assessment")
        try:
            s_geo = geolocator.geocode(start, timeout=5)
            f_geo = geolocator.geocode(finish, timeout=5)
            if s_geo and f_geo:
                return ((s_geo.latitude, s_geo.longitude), (f_geo.latitude, f_geo.longitude))
        except Exception as e:
            print(f"Geocoding error: {e}")
        return None

    def _fetch_osrm_route(self, start: tuple, finish: tuple) -> dict | None:
        """Fetches the driving polyline from OSRM to map the exact path of the truck."""
        url = f"http://router.project-osrm.org/route/v1/driving/{start[1]},{start[0]};{finish[1]},{finish[0]}?overview=full&geometries=geojson"
        try:
            resp = requests.get(url, timeout=10)
            if resp.status_code == 200 and resp.json().get('code') == 'Ok':
                return resp.json()['routes'][0]
        except Exception as e:
            print(f"OSRM Route error: {e}")
        return None

    def _find_stations_near_route(self, route_points: List[Tuple[float, float]], buffer_miles: float = 10.0) -> List[Dict]:
        """
        Calculates which gas stations are actually near the highway.
        Uses a bounding box first for DB performance, then refines with Haversine distance.
        """
        lats = [p[0] for p in route_points]
        lons = [p[1] for p in route_points]
        
        # ~0.2 degrees is roughly 10-15 miles depending on latitude
        bbox_buffer = 0.2 
        
        # Fast DB filter to exclude the rest of the country
        candidate_qs = TruckStop.objects.filter(
            latitude__gte=min(lats) - bbox_buffer,
            latitude__lte=max(lats) + bbox_buffer,
            longitude__gte=min(lons) - bbox_buffer,
            longitude__lte=max(lons) + bbox_buffer
        ).exclude(latitude__isnull=True)

        valid_stops = []
        
        # Calculate cumulative distance along the route so we know WHERE the stop is
        route_cum_dist = [0.0]
        for i in range(1, len(route_points)):
            route_cum_dist.append(route_cum_dist[-1] + self._haversine(route_points[i-1], route_points[i]))

        for stop in candidate_qs:
            stop_pt = (stop.latitude, stop.longitude)
            min_dist = float('inf')
            route_dist_marker = 0.0

            # Find the closest point on the route to this specific gas station
            for idx, pt in enumerate(route_points):
                d = self._haversine(stop_pt, pt)
                if d < min_dist:
                    min_dist = d
                    route_dist_marker = route_cum_dist[idx]

            if min_dist <= buffer_miles:
                valid_stops.append({
                    'id': stop.opis_id,
                    'name': stop.name,
                    'city': stop.city,
                    'state': stop.state,
                    'price': stop.retail_price,
                    'lat': stop.latitude,
                    'lon': stop.longitude,
                    'route_dist': route_dist_marker
                })

        # Sort stations sequentially from start to finish
        return sorted(valid_stops, key=lambda x: x['route_dist'])

    def _optimize_fuel_stops(self, stations: List[Dict], total_dist: float) -> Tuple[List[Dict], float, bool]:
        """
        Calculates optimal fuel stops for long-haul trucking:
        - Max vehicle range: 500 miles (10 MPG, 50-gallon capacity)
        - Starts with a full tank (500-mile range).
        - Drives directly to the cheapest station reachable on current fuel before filling up.
        """
        MAX_RANGE = 500.0
        MPG = 10.0
        TANK_CAP_GAL = MAX_RANGE / MPG

        current_dist = 0.0
        current_fuel_gal = TANK_CAP_GAL  # Start with full tank
        total_cost = 0.0
        plan = []

        while current_dist + (current_fuel_gal * MPG) < total_dist:
            # 1. Stations reachable on CURRENT fuel in tank
            current_reach = current_dist + (current_fuel_gal * MPG)
            reachable_now = [s for s in stations if current_dist < s['route_dist'] <= current_reach]

            # 2. Stations reachable if we fill up (within 500 miles)
            max_reach = current_dist + MAX_RANGE
            reachable_max = [s for s in stations if current_dist < s['route_dist'] <= max_reach]

            if not reachable_max:
                return [], 0.0, False  # Fuel gap > 500 miles

            # Find the absolute cheapest station in 500-mile max range
            cheapest_in_max = min(reachable_max, key=lambda x: x['price'])

            # If we can reach that overall cheapest station on current fuel, drive straight there!
            if cheapest_in_max in reachable_now:
                target = cheapest_in_max
            elif reachable_now:
                # Otherwise, drive to the cheapest station we CAN reach right now to refuel
                target = min(reachable_now, key=lambda x: x['price'])
            else:
                # Emergency fallback if current tank is empty: pick farthest reachable stop
                target = max(reachable_max, key=lambda x: x['route_dist'])

            # Drive to the chosen target station
            dist_to_target = target['route_dist'] - current_dist
            fuel_used = dist_to_target / MPG
            current_fuel_gal -= fuel_used
            current_dist = target['route_dist']

            # Fill the tank completely at the target stop
            gallons_to_buy = TANK_CAP_GAL - current_fuel_gal
            cost = gallons_to_buy * target['price']
            total_cost += cost
            current_fuel_gal = TANK_CAP_GAL  # Tank is full again

            plan.append({
                "station_id": target['id'],
                "name": f"{target['name']} ({target['city']}, {target['state']})",
                "price_per_gallon": target['price'],
                "gallons_bought": round(gallons_to_buy, 2),
                "cost_for_stop": round(cost, 2),
                "stop_distance_from_start": round(target['route_dist'], 2)
            })

        return plan, total_cost, True

    def _haversine(self, coord1: tuple, coord2: tuple) -> float:
        """Math helper to calculate straight-line distance between two earth coordinates in miles."""
        R = 3958.8 
        lat1, lon1 = math.radians(coord1[0]), math.radians(coord1[1])
        lat2, lon2 = math.radians(coord2[0]), math.radians(coord2[1])
        dlat, dlon = lat2 - lat1, lon2 - lon1
        a = math.sin(dlat / 2)**2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2)**2
        return R * (2 * math.atan2(math.sqrt(a), math.sqrt(1 - a)))

    def _generate_folium_map(self, start: tuple, finish: tuple, route_geo: dict, plan: list) -> str:
        """Renders an interactive HTML map for the UI."""
        m = folium.Map(
            location=[start[0], start[1]], 
            zoom_start=6, 
            tiles="https://server.arcgisonline.com/ArcGIS/rest/services/World_Street_Map/MapServer/tile/{z}/{y}/{x}",
            attr="Esri"
        )
        folium.GeoJson(route_geo, style_function=lambda x: {'color': '#1E88E5', 'weight': 4}).add_to(m)
        folium.Marker(start, popup="Start", icon=folium.Icon(color="green")).add_to(m)
        folium.Marker(finish, popup="Finish", icon=folium.Icon(color="red")).add_to(m)
        
        for idx, stop in enumerate(plan, 1):
            station = TruckStop.objects.get(opis_id=stop['station_id'])
            html = f"<b>Stop #{idx}: {stop['name']}</b><br>Cost: ${stop['cost_for_stop']}<br>Gallons: {stop['gallons_bought']}"
            folium.Marker(
                [station.latitude, station.longitude], 
                popup=html, 
                icon=folium.Icon(color="orange", icon="info-sign")
            ).add_to(m)
            
        m.save("demo_map.html")
        return m._repr_html_()