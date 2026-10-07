# FuelSeek Routing API - Backend Engineer Assessment

An intelligent API that calculates the most cost-effective fuel stops for a truck traveling across the USA. Built with Django and Django REST Framework.

## 🚀 The Approach

Rather than simply finding gas stations and stopping whenever the tank hits zero, this API implements a **Greedy Algorithm with Look-Ahead Logic** to genuinely save fleets money. 

### Core Features & Optimizations
1. **The Fuel Optimization Algorithm**: The truck doesn't blindly fill up. If it is currently at a station charging $3.50/gal, but there is a station 200 miles ahead charging $3.00/gal, the algorithm calculates exactly how much fuel to buy to *just reach* the cheaper station, where it will then fill up completely.
2. **Minimal External API Calls**: To ensure lightning-fast response times, the API hits the free OSRM (Open Source Routing Machine) API exactly **once** to get the route geometry. 
3. **Data Pre-Processing**: Instead of geocoding 8,000+ CSV rows on the fly, a custom Django Management Command parses, deduplicates (via in-memory city caching), and geocodes the stations into a SQLite/Postgres database ahead of time.
4. **Spatial Proximity Filtering**: The API uses a bounding-box query to filter thousands of stations down to only those near the route *before* running expensive Haversine distance calculations in Python.

## 🛠 Installation & Setup

You can set up the entire project, run migrations, and execute the data ingestion script with a single command. 

Ensure you have Python 3 installed, then run the setup script from the root directory:

```bash
chmod +x setup.sh
./setup.sh