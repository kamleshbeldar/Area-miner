# Area Business Miner (Area Miner / AM)

A geographic business intelligence tool that scrapes Google Maps data for user-defined areas without requiring a Google Maps API key.

## Overview

Area Business Miner is a web application that allows users to draw custom geographic areas on a map and automatically collect comprehensive business data from Google Maps. The system uses a two-phase scraping pipeline (discovery → detail) with persistent task queuing for reliable, scalable data collection.

## Key Features

- **Map-Based Area Selection**: Draw circles, rectangles, or polygons on OpenStreetMap (Leaflet.js)
- **Two-Phase Scraping Pipeline**:
  - Discovery Phase: Collect place URLs from Google Maps searches
  - Detail Phase: Extract detailed business information for each unique place
- **Persistent Task Queue**: SQLite WAL database for crash recovery and concurrent worker access
- **Browser-Based Scraping**: Uses Scrapling + Playwright (no official Google Maps API required)
- **Configurable Parameters**: Adjust workers, rate limits, delays, grid spacing, and coverage depth
- **Data Enrichment**: Extract emails, social media profiles, and website technologies
- **Lead Scoring**: Automatic scoring based on contact information completeness
- **Real-time Monitoring**: Live progress tracking and logs
- **Flexible Export**: CSV or JSONL export at any time
- **Duplicate Detection**: Automatic deduplication by place_id, phone, website, or name/address

## Technical Stack

- **Backend**: Python 3.13, Flask 3
- **Browser Automation**: Scrapling + Playwright (Chromium)
- **Database**: SQLite with WAL mode
- **Frontend**: Leaflet.js (OpenStreetMap), HTML/CSS/JavaScript
- **Templates**: Jinja2
- **Dependencies**: 
  - `flask>=3.1.3`
  - `scrapling>=0.4.11`
  - `playwright>=1.61.0`
  - `rebrowser-playwright>=1.52.0`
  - `camoufox>=0.4.11`
  - `curl-cffi>=0.15.0`
  - `msgspec>=0.21.1`

## Project Structure

```
Maps_area_scraper/
├── maps_area_scraper/                  # Main Flask application
│   ├── app.py                         # Flask routes & worker management
│   ├── area_engine.py                 # Core scraping logic (grid, discovery, detail)
│   ├── queue_store.py                 # SQLite job queue & persistence
│   ├── templates/                     # HTML templates
│   │   ├── index.html                 # Main map interface
│   │   └── progress.html              # Job monitoring dashboard
│   ├── static/                        # CSS, JS, images
│   │   ├── styles.css                 # Main stylesheet
│   │   └── app.js                     # Frontend logic
│   ├── area_scale.db                  # SQLite database (22MB+)
│   └── ...                            # Supporting files
├── maps_lead_studio/                  # Scraper implementation
│   └── scraper.py                     # Google Maps interaction logic
├── main.py                            # Simple entry point
├── pyproject.toml                     # Python dependencies
├── package.json                       # Node.js workspace config
├── replit.md                          # Project documentation
└── ...                                # Configuration files
```

## Architecture Details

### Two-Phase Pipeline

1. **Discovery Phase**
   - Grid points generated within the user-defined area
   - Each grid point searched for businesses in specified categories
   - Place URLs collected and deduplicated
   - Results stored in `area_places` table

2. **Detail Phase**
   - Unique place URLs processed for detailed business information
   - Data extracted: name, category, rating, reviews, contact info, hours, etc.
   - Website enrichment for emails, social media, and technologies
   - Lead scoring based on contact completeness
   - Results stored in `area_leads` table

### Persistent Queue System

- SQLite database with WAL mode for concurrent access
- Tables: `area_jobs`, `area_tasks`, `area_places`, `area_leads`, `area_logs`
- Automatic recovery of interrupted jobs
- Worker coordination through database status flags
- Configurable retry mechanisms for failed operations

### Database Schema

**area_jobs**: Job metadata and configuration
**area_tasks**: Discovery tasks (grid point + category combinations)
**area_places**: Discovered places awaiting detail processing
**area_leads**: Final extracted business data
**area_logs**: Real-time processing logs

## Installation & Setup

### Prerequisites
- Python 3.12+
- Node.js/pnpm (for workspace dependencies)
- Playwright browsers: `python3 -m playwright install chromium`

### Installation
```bash
# Clone repository
git clone <repository-url>
cd Maps_area_scraper

# Install Python dependencies
pip install -e .

# Install Playwright browsers
python3 -m playwright install chromium

# Install Node.js dependencies (if needed)
pnpm install
```

### Running the Application
```bash
# Method 1: Direct execution
cd maps_area_scraper
python3 app.py

# Method 2: Via Replit workflow
# (Use the "Area Business Miner" workflow in Replit)
```

The application will be available at `http://localhost:5000`

## Usage Guide

1. **Open the Application**: Navigate to `http://localhost:5000`
2. **Draw Your Area**: Use the map tools to draw a circle, rectangle, or polygon around your target area
3. **Configure Settings**:
   - **Coverage**: Quick (common categories) or Deep (extensive category list)
   - **Custom Categories**: Specify additional business types to search
   - **Max Results**: Target number of businesses to collect
   - **Results Per Query**: Businesses per Google Maps search (1-40)
   - **Grid Spacing**: Distance between search points (0.15-25 km)
   - **Max Queries**: Maximum search queries to execute
   - **Workers**: Parallel browser instances (1-8)
   - **Rate Limit**: Requests per minute across all workers (1-240)
   - **Browser Options**: Headless mode, real Chrome, website enrichment
4. **Start the Scan**: Click "Create queue & start"
5. **Monitor Progress**: Watch real-time stats, logs, and progress bars
6. **Export Results**: Download CSV or JSONL when complete or at any time
7. **Manage Jobs**: Resume stopped jobs, retry failed items, or start new searches

## Data Output

Exported data includes:
- Business name, category, rating, review count
- Phone number, email, website
- Address, business hours, price range
- Plus code, latitude/longitude, place ID
- Google Maps URL
- Social media profiles (Facebook, Instagram, LinkedIn, YouTube, Twitter/X)
- WhatsApp number
- Contact page URL
- Detected website technologies
- Lead score (0-100) and tier (Hot/Warm/Cold)
- Discovery metadata (grid coordinates, search category)

## Architecture Decisions

1. **Two-Phase Pipeline**: Separates URL discovery from detail extraction for efficiency and fault tolerance
2. **Persistent Queue**: SQLite WAL enables crash recovery and concurrent access without external dependencies
3. **Shared Rate Limiting**: Central rate gate prevents overwhelming target sites while maximizing throughput
4. **Per-Run Deduplication**: Avoids duplicate processing within each job while allowing cross-job duplication
5. **Browser Automation**: Avoids API limitations and costs while capturing dynamically loaded content
6. **Modular Design**: Clear separation between web interface, core logic, and data persistence

## Limitations & Considerations

- **Google Maps Terms of Service**: Scraping Google Maps may violate their ToS; use responsibly and consider rate limiting
- **Data Completeness**: Google Maps doesn't guarantee complete business listings for any area
- **Blocking Risk**: Aggressive scraping may trigger IP-based blocking; use reasonable rate limits
- **JavaScript Dependency**: Requires modern browser capabilities for dynamic content rendering
- **Storage Requirements**: Database grows with collected data; periodic cleanup may be needed

## Data Ethics & Compliance

This tool is intended for legitimate business research and lead generation purposes. Users should:
- Respect robots.txt and website terms of service
- Implement appropriate rate limiting to avoid overloading target servers
- Use collected data in compliance with applicable data protection regulations (GDPR, CCPA, etc.)
- Consider ethical implications of business data collection and usage

## Customization & Extension

### Adding New Data Fields
Modify the `DETAIL_SCRIPT` in `scraper.py` to extract additional data points from Google Maps pages.

### Changing Categories
Edit the `QUICK_CATEGORIES` and `DEEP_CATEGORIES` lists in `area_engine.py`.

### Adjusting Scoring Algorithm
Modify the `_score` function in `scraper.py` to change how lead scores are calculated.

### Alternative Data Sources
Replace the Google Maps scraping logic in `scraper.py` with other local business data sources.

## Troubleshooting

### Common Issues

1. **Playwright Not Installed**: Run `python3 -m playwright install chromium`
2. **Database Lock Errors**: Ensure no other processes are accessing the database file
3. **Slow Performance**: Reduce worker count or increase delays between requests
4. **Missing Data**: Verify website enrichment is enabled and adjust timeouts if needed
5. **Job Stuck**: Use the "Resume" or "Retry failed" buttons in the job interface

### Logs & Monitoring
- Real-time logs visible in the job progress page
- Detailed logs stored in `area_logs` table
- Database file (`area_scale.db`) can be inspected with SQLite tools

## License

MIT License - see LICENSE file for details.

---
*Area Business Miner - Extract business intelligence from Google Maps without API keys*