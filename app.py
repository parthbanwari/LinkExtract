import os
from dotenv import load_dotenv
import google.generativeai as genai
import requests
from bs4 import BeautifulSoup
from flask import Flask, request, jsonify, render_template, send_from_directory
import psycopg2
from psycopg2.extras import RealDictCursor
import json

# Load environment variables from .env file
load_dotenv()

# Set up the Flask app with proper template and static file directories
app = Flask(__name__, 
            static_folder='.', 
            static_url_path='',
            template_folder='.')

# Configure the Gemini API with the key from .env
api_key = os.getenv("GOOGLE_API_KEY")
if not api_key:
    raise ValueError("No GOOGLE_API_KEY found in .env file")

# Configure PostgreSQL connection
db_config = {
    "host": os.getenv("DB_HOST", "localhost"),
    "database": os.getenv("DB_NAME", "job_data"),
    "user": os.getenv("DB_USER", "postgres"),
    "password": os.getenv("DB_PASSWORD", "Admin"),
    "port": os.getenv("DB_PORT", "5432")
}

genai.configure(api_key=api_key)
model = genai.GenerativeModel('gemini-1.5-pro')

def get_db_connection():
    """Create a connection to the PostgreSQL database"""
    conn = psycopg2.connect(**db_config)
    return conn

def init_db():
    """Initialize the database and create tables if they don't exist"""
    conn = get_db_connection()
    cur = conn.cursor()
    
    # Create the jobs table if it doesn't exist
    cur.execute('''
    CREATE TABLE IF NOT EXISTS jobs (
        id SERIAL PRIMARY KEY,
        job_title TEXT,
        company_name TEXT,
        required_skills TEXT,
        publication_date TEXT,
        author TEXT,
        url TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    ''')
    
    conn.commit()
    cur.close()
    conn.close()

# Initialize the database when the app starts
init_db()

@app.route('/', methods=['GET'])
def index():
    return render_template('index.html')

@app.route('/analyze', methods=['POST'])
def analyze_link():
    data = request.get_json()
    url = data.get('url')
    
    if not url:
        return jsonify({'error': 'No URL provided'}), 400
    
    try:
        print(f"Attempting to fetch content from: {url}")
        content = get_page_content(url)
        
        # If we detected the login wall
        if "LinkedIn requires authentication" in content:
            return jsonify({
                'error': 'LinkedIn login required',
                'message': 'The application cannot access this LinkedIn content without authentication.',
                'parsed_data': {
                    'job_title': 'not found (LinkedIn authentication required)',
                    'company_name': 'not found (LinkedIn authentication required)', 
                    'required_skills': 'not found (LinkedIn authentication required)',
                    'publication_date': 'not found (LinkedIn authentication required)',
                    'author': 'not found (LinkedIn authentication required)',
                    'url': url
                }
            }), 403
            
        print(f"Successfully fetched content, length: {len(content)}")
        gemini_response = generate_gemini_response(content, url)
        print("Gemini response:", gemini_response)  # Add this to see raw Gemini response
        
        # Parse the Gemini response to extract structured data
        parsed_data = parse_gemini_response(gemini_response)
        print("Parsed data:", parsed_data)  # Add this to see what was parsed
        parsed_data['url'] = url  # Add the URL to the parsed data
        
        return jsonify({
            'results': gemini_response,
            'parsed_data': parsed_data
        })
    except Exception as e:
        print(f"Error in analyze_link: {str(e)}")
        return jsonify({'error': str(e)}), 500

# Improve the request headers to better mimic a real browser
def get_page_content(url):
    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.5',
            'Accept-Encoding': 'gzip, deflate, br',
            'Connection': 'keep-alive',
            'Upgrade-Insecure-Requests': '1',
            'Cache-Control': 'max-age=0'
        }
        
        # Add logging to debug the response
        print(f"Fetching URL: {url}")
        response = requests.get(url, headers=headers, timeout=20)
        print(f"Response status code: {response.status_code}")
        
        # If LinkedIn redirects to login page, it will contain this string
        if "authwall" in response.url or "signup" in response.url:
            print("LinkedIn login wall detected")
            return "LinkedIn requires authentication to access this content. Unable to extract job data."
            
        response.raise_for_status()
        soup = BeautifulSoup(response.content, "html.parser")
        
        # Log page title for debugging
        print(f"Page title: {soup.title.string if soup.title else 'No title found'}")
        
        # For LinkedIn, extract specific parts that might contain job information
        main_content = soup.find('div', {'class': 'feed-shared-update-v2'})
        if main_content:
            return main_content.get_text(strip=True)
        
        # Fall back to whole page if specific content not found
        return soup.get_text(strip=True)[:100000]
    except requests.exceptions.RequestException as e:
        raise Exception(f"Error fetching URL: {e}")
    
def generate_gemini_response(content, url):
    prompt = f"""
You are a web page data extractor specializing in LinkedIn job posts. Analyze the content of the following LinkedIn page and extract specific information into a structured format.

Web Page URL: {url}

Web Page Content:
---
{content}
---

Specifically look for job-related information in the content. The URL suggests this is a LinkedIn post about a job opening, so try to identify relevant details even if they're not formatted as a traditional job posting. 

For LinkedIn posts, the text might be more conversational, so look for phrases like "we're hiring", "job opening", "looking for", etc.

Please extract the information according to the following constraints and format it as a table. If you cannot find a specific piece of information, mark it as "not found".

| Column Name      | Description                                          | Extraction Guidance                                                                 |
| ---------------- | ---------------------------------------------------- | ------------------------------------------------------------------------------------ |
| Job Title        | The title of the job.                                | Look for position names or roles mentioned. If multiple jobs, list the first one.    |
| Company Name     | The name of the company posting the job.             | Try to extract from the post author or content. For LinkedIn, check for mentions.    |
| Required Skills  | A list of skills needed for the job.                 | Look for "skills", "requirements", "qualifications", or similar sections.            |
| Publication Date | The date the job posting or article was published.   | Try to extract date information. Format as YYYY-MM-DD if possible.                   |
| Author           | The author of the article.                           | Usually the person who posted the content on LinkedIn.                               |

Present the information only as a standard markdown table with a header row and one data row. Don't include any other text or explanation.
"""
    try:
        response = model.generate_content(prompt)
        return response.text
    except Exception as e:
        raise Exception(f"Gemini API Error: {e}")

def parse_gemini_response(response_text):
    """Parse the markdown table response from Gemini into a dictionary"""
    # Default values
    result = {
        "job_title": "not found",
        "company_name": "not found", 
        "required_skills": "not found",
        "publication_date": "not found",
        "author": "not found"
    }
    
    try:
        # First attempt: Try to parse as a markdown table
        lines = [line.strip() for line in response_text.split('\n') if line.strip()]
        
        # Find rows that contain table data (lines with | character)
        table_rows = [line for line in lines if '|' in line]
        
        if len(table_rows) >= 3:  # Header, separator, and at least one data row
            # Process the first data row after header and separator
            for i, row in enumerate(table_rows):
                if i > 1:  # Skip header and separator
                    # This is our data row
                    cells = [cell.strip() for cell in row.split('|') if cell.strip()]
                    headers = [header.strip() for header in table_rows[0].split('|') if header.strip()]
                    
                    # Map headers to values
                    for j, header in enumerate(headers):
                        if j < len(cells):
                            header_lower = header.lower()
                            value = cells[j]
                            
                            if 'job title' in header_lower or 'position' in header_lower:
                                result['job_title'] = value
                            elif 'company' in header_lower:
                                result['company_name'] = value
                            elif 'skill' in header_lower or 'requirement' in header_lower or 'qualif' in header_lower:
                                result['required_skills'] = value
                            elif 'date' in header_lower or 'published' in header_lower:
                                result['publication_date'] = value
                            elif 'author' in header_lower or 'poster' in header_lower:
                                result['author'] = value
                    
                    break  # We only need the first data row
        
        # If we couldn't find values through table parsing, try regex as fallback
        if all(val == "not found" for val in result.values()):
            import re
            
            # Try to find job title
            title_patterns = [
                r'\|\s*(?:Job Title|Title|Position|Role)\s*\|\s*([^|]+)\s*\|',
                r'Job Title:?\s*([^\n]+)'
            ]
            for pattern in title_patterns:
                match = re.search(pattern, response_text, re.IGNORECASE)
                if match:
                    result["job_title"] = match.group(1).strip()
                    break
            
            # Try to find company name
            company_patterns = [
                r'\|\s*(?:Company Name|Company|Organization)\s*\|\s*([^|]+)\s*\|',
                r'Company:?\s*([^\n]+)'
            ]
            for pattern in company_patterns:
                match = re.search(pattern, response_text, re.IGNORECASE)
                if match:
                    result["company_name"] = match.group(1).strip()
                    break
            
            # Try to find required skills
            skills_patterns = [
                r'\|\s*(?:Required Skills|Skills|Requirements|Qualifications)\s*\|\s*([^|]+)\s*\|',
                r'Skills:?\s*([^\n]+)',
                r'Requirements:?\s*([^\n]+)'
            ]
            for pattern in skills_patterns:
                match = re.search(pattern, response_text, re.IGNORECASE)
                if match:
                    result["required_skills"] = match.group(1).strip()
                    break
            
            # Try to find publication date
            date_patterns = [
                r'\|\s*(?:Publication Date|Date|Published|Posted)\s*\|\s*([^|]+)\s*\|',
                r'Date:?\s*([^\n]+)',
                r'Published:?\s*([^\n]+)'
            ]
            for pattern in date_patterns:
                match = re.search(pattern, response_text, re.IGNORECASE)
                if match:
                    result["publication_date"] = match.group(1).strip()
                    break
            
            # Try to find author
            author_patterns = [
                r'\|\s*(?:Author|Posted By|Creator)\s*\|\s*([^|]+)\s*\|',
                r'Author:?\s*([^\n]+)',
                r'Posted by:?\s*([^\n]+)'
            ]
            for pattern in author_patterns:
                match = re.search(pattern, response_text, re.IGNORECASE)
                if match:
                    result["author"] = match.group(1).strip()
                    break
        
        # Clean up any "not found" variations or empty values
        for key, value in result.items():
            if not value or value.lower() in ['not found', 'n/a', '-']:
                result[key] = "not found"
                
        return result
    except Exception as e:
        print(f"Error parsing Gemini response: {str(e)}")
        print(f"Response text: {response_text}")
        return result

@app.route('/delete_job/<int:job_id>', methods=['POST'])
def delete_job(job_id):
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        
        # Delete the job from the database
        cur.execute('DELETE FROM jobs WHERE id = %s', (job_id,))
        
        conn.commit()
        cur.close()
        conn.close()
        
        return jsonify({'success': True})
    except Exception as e:
        print(f"Error deleting job: {str(e)}")
        return jsonify({'error': str(e)}), 500

@app.route('/save_job', methods=['POST'])
def save_job():
    data = request.get_json()
    
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        
        # Insert the job data into the database
        cur.execute('''
        INSERT INTO jobs (job_title, company_name, required_skills, publication_date, author, url)
        VALUES (%s, %s, %s, %s, %s, %s)
        RETURNING id
        ''', (
            data.get('job_title', 'not found'),
            data.get('company_name', 'not found'),
            data.get('required_skills', 'not found'),
            data.get('publication_date', 'not found'),
            data.get('author', 'not found'),
            data.get('url', '')
        ))
        
        job_id = cur.fetchone()[0]
        conn.commit()
        cur.close()
        conn.close()
        
        return jsonify({'success': True, 'id': job_id})
    except Exception as e:
        print(f"Error saving job: {str(e)}")
        return jsonify({'error': str(e)}), 500

@app.route('/jobs', methods=['GET'])
def get_jobs():
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        
        cur.execute('SELECT * FROM jobs ORDER BY created_at DESC')
        jobs = cur.fetchall()
        
        cur.close()
        conn.close()
        
        return jsonify({'jobs': jobs})
    except Exception as e:
        print(f"Error fetching jobs: {str(e)}")
        return jsonify({'error': str(e)}), 500

if __name__ == "__main__":
    print("Starting Job Data Extractor application...")
    print(f"App will be available at http://127.0.0.1:5000")
    app.run(debug=True, host='0.0.0.0', port=5000)