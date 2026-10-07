#!/bin/bash

# Exit immediately if any command fails
set -e

echo "🚀 Starting database setup and data ingestion..."

# Check if the virtual environment exists and activate it
if [ -d "venv" ]; then
    echo "📦 Activating virtual environment..."
    source venv/bin/activate
else
    echo "⚠️  Warning: 'venv' directory not found. Falling back to global python."
fi

# Ensure manage.py exists in the current directory
if [ ! -f "manage.py" ]; then
    echo "❌ Error: manage.py not found. Please run this script from the project root."
    exit 1
fi

echo "🛠️  Creating database migrations..."
python manage.py makemigrations api

echo "🏗️  Applying migrations..."
python manage.py migrate

echo "⛽ Loading truck stop data from CSV..."
python manage.py load_truckstop

echo "✅ All steps completed successfully!"