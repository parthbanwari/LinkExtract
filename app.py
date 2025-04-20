import os
from dotenv import load_dotenv
import google.generativeai as genai
import requests
from bs4 import BeautifulSoup
from flask import Flask, request, jsonify, render_template, send_from_directory
import psycopg2
from psycopg2.extras import RealDictCursor
import json
import re  
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
        experience_required TEXT,
        salary_range TEXT,
        location TEXT,
        additional_info JSONB,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    ''')
    
    conn.commit()
    cur.close()
    conn.close()

# Initialize the database when the app starts
init_db()

def add_missing_columns():
    """Add missing columns to the jobs table"""
    conn = get_db_connection()
    cur = conn.cursor()
    
    # Add missing columns if they don't exist
    cur.execute('''
    ALTER TABLE jobs 
    ADD COLUMN IF NOT EXISTS experience_required TEXT,
    ADD COLUMN IF NOT EXISTS salary_range TEXT,
    ADD COLUMN IF NOT EXISTS location TEXT,
    ADD COLUMN IF NOT EXISTS additional_info JSONB DEFAULT '{}'::jsonb;
    ''')
    
    conn.commit()
    cur.close()
    conn.close()

# Call this function after init_db()
add_missing_columns()

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
        
        # Get the HTML content for extracting author profile URL
        html_content = get_raw_html(url)
        
        gemini_response = generate_gemini_response(content, url)
        print("Gemini response:", gemini_response)  # Add this to see raw Gemini response
        
        # Parse the Gemini response to extract structured data
        parsed_data = parse_gemini_response(gemini_response)
        print("Parsed data:", parsed_data)  # Add this to see what was parsed
        parsed_data['url'] = url  # Add the URL to the parsed data
        
        # Extract author profile URL from the HTML content
        author_profile_url = extract_author_profile_url(html_content, url)
        if author_profile_url:
            parsed_data['author_profile_url'] = author_profile_url
        
        return jsonify({
            'results': gemini_response,
            'parsed_data': parsed_data,
            'raw_content': content  # Now this will be just text
        })
    except Exception as e:
        print(f"Error in analyze_link: {str(e)}")
        return jsonify({'error': str(e)}), 500

def get_raw_html(url):
    """Get the raw HTML content for link extraction"""
    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        }
        response = requests.get(url, headers=headers, timeout=20)
        response.raise_for_status()
        return response.text
    except:
        return ""

# Improve the request headers to better mimic a real browser
# In the get_page_content function, add type checking before replacing content

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
        
        print(f"Fetching URL: {url}")
        response = requests.get(url, headers=headers, timeout=20)
        print(f"Response status code: {response.status_code}")
        
        if "authwall" in response.url or "signup" in response.url:
            print("LinkedIn login wall detected")
            return "LinkedIn requires authentication to access this content. Unable to extract job data."
            
        response.raise_for_status()
        soup = BeautifulSoup(response.content, "html.parser")
        
        # First, try to find the post content
        post_text_elements = soup.select(
            '.feed-shared-update-v2__description, '
            '.feed-shared-text, '
            '.feed-shared-update-v2__commentary, '
            '.share-update-card__update-text, '
            '.feed-shared-text__text-view'
        )
        
        if post_text_elements:
            post_content = ' '.join([element.get_text(strip=True) for element in post_text_elements])
            
            # Ensure post_content is a string
            if not isinstance(post_content, str):
                post_content = str(post_content)
                
            # Clean up the text
            post_content = re.sub(r'\s+', ' ', post_content)
            
            # Preserve bullet points
            post_content = post_content.replace('• ', '\n• ')
            post_content = post_content.replace('•', '\n•')
            
            # Remove common LinkedIn text patterns at the beginning
            post_content = re.sub(r'^.*?(Skip to main content)', '', post_content)
            
            # Remove comment section indicators
            post_content = re.sub(r'\d+ Comments.*?(Share|Copy|LinkedIn|Facebook|Twitter)', '', post_content)
            
            return post_content.strip()
        
        # If we couldn't extract post content with the specific selectors, 
        # try to get just the main content without comments
        main_content = soup.select_one('main, .core-rail, .scaffold-layout__main')
        if main_content:
            # Remove comments section and social actions before extracting text
            for element in main_content.select('.comments-comments-list, .social-details-social-counts, ' +
                                               '.feed-shared-social-actions, .comments-comment-item, ' +
                                               '.feed-shared-comment-list, .social-details-social-activity'):
                element.extract()
            
            # Get just the text of what remains
            content = main_content.get_text(strip=True, separator=' ')
            
            # Ensure content is a string
            if not isinstance(content, str):
                content = str(content)
                
            # Clean up UI text
            content = re.sub(r'Agree & Join LinkedIn.*?Cookie Policy \. Skip to main content', '', content)
            content = re.sub(r'\d+ Comments Like Comment Share Copy LinkedIn Facebook Twitter', '', content)
            content = re.sub(r'Report this post', '', content)
            
            # Format bullet points
            content = content.replace('• ', '\n• ')
            content = content.replace('•', '\n•')
            
            return content.strip()
            
        # Last resort - try to get just the text without comments
        body = soup.body
        if body:
            # Remove all comment sections, social actions, etc.
            for unwanted in body.select('.comments-comments-list, .comments-container, .social-footer-stats-container, ' +
                                        '.feed-shared-social-actions, .comments-comment-item, .feed-shared-comment-list'):
                unwanted.extract()
                
            content = body.get_text(strip=True, separator=' ')
            
            # Ensure content is a string
            if not isinstance(content, str):
                content = str(content)
                
            content = re.sub(r'Agree & Join LinkedIn.*?Cookie Policy \. Skip to main content', '', content)
            content = re.sub(r'\d+ Comments Like Comment Share Copy LinkedIn Facebook Twitter', '', content)
            content = re.sub(r'Report this post', '', content)
            
            # Clean up spaces and preserve bullet points
            content = re.sub(r'\s+', ' ', content)
            content = content.replace('• ', '\n• ')
            content = content.replace('•', '\n•')
            
            return content.strip()
            
        return "Could not extract content from the LinkedIn page."
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

Specifically look for job-related information in the content. Extract as much detail as possible, even if the format is conversational rather than a traditional job posting.

If the content contains bullet points with skills, technical requirements, or research areas, make sure to capture those in the Required Skills section.

Please extract the information according to the following constraints and format it as a table. If you cannot find a specific piece of information, mark it as "not found".

| Column Name      | Description                                          | Extraction Guidance                                                                 |
| ---------------- | ---------------------------------------------------- | ------------------------------------------------------------------------------------ |
| Job Title        | The title of the job.                                | Look for position names or roles mentioned. If multiple jobs, list all of them.      |
| Company Name     | The name of the company posting the job.             | Try to extract from the post author or content. For LinkedIn, check for mentions.    |
| Required Skills  | A list of skills needed for the job.                 | Look for "skills", "requirements", "qualifications", or bullet points in the content.|
| Publication Date | The date the job posting or article was published.   | Try to extract date information. Format as YYYY-MM-DD if possible.                   |
| Author           | The author of the article.                           | Usually the person who posted the content on LinkedIn.                               |
| Experience       | Years of experience required for the role.           | Look for mentions of education or experience requirements, including PhD references. |
| Salary          | Compensation or salary information.                  | Look for any mentions of salary, pay range, or compensation.                         |
| Location        | Where the job is located.                           | Look for city, state, country or remote work information.                           |
| Job Type        | Full-time, part-time, contract, etc.                | Extract the employment type if mentioned.                                            |
| Education       | Required education level or degree.                  | Look for educational requirements, especially PhD or specialized degrees.           |
| Research Focus  | The research focus or specialty areas.              | Look for mentions of specialized research areas or domains.                          |

Present the information only as a standard markdown table with a header row and one data row. Don't include any other text or explanation.
"""
    try:
        response = model.generate_content(prompt)
        return response.text
    except Exception as e:
        raise Exception(f"Gemini API Error: {e}")

def extract_author_profile_url(content, job_url):
    """Try to extract the author profile URL from LinkedIn content"""
    try:
        # Parse the URL to get the base domain
        from urllib.parse import urlparse
        parsed_url = urlparse(job_url)
        linkedin_domain = f"{parsed_url.scheme}://{parsed_url.netloc}"
        
        # Process the HTML content
        soup = BeautifulSoup(content, "html.parser")
        
        # Look for profile links with priority for author links 
        profile_links = soup.find_all('a', href=lambda href: href and ('/in/' in href))
        
        if profile_links:
            # Get the first profile link (usually the author)
            href = profile_links[0]['href']
            
            # If it's a relative URL, make it absolute
            if href.startswith('/'):
                return linkedin_domain + href
            return href
        else:
            # Try to extract LinkedIn profile URLs using regex
            import re
            profile_pattern = r'(https?://(?:www\.)?linkedin\.com/in/[\w-]+)'
            match = re.search(profile_pattern, content)
            if match:
                return match.group(1)
                
        return None
    except Exception as e:
        print(f"Error extracting author profile URL: {e}")
        return None

def parse_gemini_response(response_text):
    """Parse the markdown table response from Gemini into a dictionary"""
    # Default values for main fields
    result = {
        "job_title": "not found",
        "company_name": "not found", 
        "required_skills": "not found",
        "publication_date": "not found",
        "author": "not found",
        # Add new important fields
        "experience_required": "not found",
        "salary_range": "not found",
        "location": "not found",
        # Add an additional field to store extra data
        "additional_info": {}
    }
    
    # Extract job title
    job_title_patterns = [
        r'\|\s*Job Title\s*\|\s*([^|]+)\s*\|',
        r'\|\s*Title\s*\|\s*([^|]+)\s*\|'
    ]
    for pattern in job_title_patterns:
        match = re.search(pattern, response_text, re.IGNORECASE)
        if match:
            result["job_title"] = match.group(1).strip()
            if result["job_title"].lower() == "not found":
                # Try to extract from the content if markdown table doesn't have it
                job_title_content_match = re.search(r'(Interns? and Full-time Applied Scientists|Full-time Applied Scientists and Interns?)', response_text)
                if job_title_content_match:
                    result["job_title"] = job_title_content_match.group(1).strip()
            break
    
    # Extract company name
    company_patterns = [
        r'\|\s*Company Name\s*\|\s*([^|]+)\s*\|',
        r'\|\s*Company\s*\|\s*([^|]+)\s*\|'
    ]
    for pattern in company_patterns:
        match = re.search(pattern, response_text, re.IGNORECASE)
        if match:
            result["company_name"] = match.group(1).strip()
            break
    
    # Extract required skills
    skills_patterns = [
        r'\|\s*Required Skills\s*\|\s*([^|]+)\s*\|',
        r'\|\s*Skills\s*\|\s*([^|]+)\s*\|'
    ]
    for pattern in skills_patterns:
        match = re.search(pattern, response_text, re.IGNORECASE)
        if match:
            result["required_skills"] = match.group(1).strip()
            if result["required_skills"].lower() == "not found":
                # Try to extract skills from bullet points in the content
                skills_list = re.findall(r'•\s*([^•\n]+)', response_text)
                if skills_list:
                    result["required_skills"] = ', '.join([skill.strip() for skill in skills_list])
            break
    
    # Extract publication date
    date_patterns = [
        r'\|\s*Publication Date\s*\|\s*([^|]+)\s*\|',
        r'\|\s*Date\s*\|\s*([^|]+)\s*\|'
    ]
    for pattern in date_patterns:
        match = re.search(pattern, response_text, re.IGNORECASE)
        if match:
            result["publication_date"] = match.group(1).strip()
            break
    
    # Extract author
    author_patterns = [
        r'\|\s*Author\s*\|\s*([^|]+)\s*\|'
    ]
    for pattern in author_patterns:
        match = re.search(pattern, response_text, re.IGNORECASE)
        if match:
            result["author"] = match.group(1).strip()
            break
    
    # Extract experience required
    experience_patterns = [
        r'\|\s*(?:Experience|Experience Required|Work Experience)\s*\|\s*([^|]+)\s*\|',
        r'Experience:?\s*([^\n]+)'
    ]
    for pattern in experience_patterns:
        match = re.search(pattern, response_text, re.IGNORECASE)
        if match:
            result["experience_required"] = match.group(1).strip()
            break
    
    # Check for PhD mention in the content for experience
    if result["experience_required"].lower() == "not found":
        phd_match = re.search(r'PhD researchers? or graduates?', response_text)
        if phd_match:
            result["experience_required"] = "PhD researchers or graduates"
            
    # Extract salary range
    salary_patterns = [
        r'\|\s*(?:Salary|Compensation|Salary Range|Pay)\s*\|\s*([^|]+)\s*\|',
        r'Salary:?\s*([^\n]+)',
        r'Compensation:?\s*([^\n]+)'
    ]
    for pattern in salary_patterns:
        match = re.search(pattern, response_text, re.IGNORECASE)
        if match:
            result["salary_range"] = match.group(1).strip()
            break
            
    # Extract location
    location_patterns = [
        r'\|\s*(?:Location|Job Location|Work Location)\s*\|\s*([^|]+)\s*\|',
        r'Location:?\s*([^\n]+)'
    ]
    for pattern in location_patterns:
        match = re.search(pattern, response_text, re.IGNORECASE)
        if match:
            result["location"] = match.group(1).strip()
            break
    
    # Extract any other information found in the response that might be useful
    other_patterns = [
        (r'\|\s*(?:Job Type|Employment Type)\s*\|\s*([^|]+)\s*\|', "job_type"),
        (r'\|\s*(?:Education|Education Required)\s*\|\s*([^|]+)\s*\|', "education"),
        (r'\|\s*(?:Benefits|Perks)\s*\|\s*([^|]+)\s*\|', "benefits"),
        (r'\|\s*(?:Application Deadline|Deadline)\s*\|\s*([^|]+)\s*\|', "deadline"),
        (r'\|\s*(?:Remote|Remote Work)\s*\|\s*([^|]+)\s*\|', "remote_options")
    ]
    
    for pattern, key in other_patterns:
        match = re.search(pattern, response_text, re.IGNORECASE)
        if match:
            value = match.group(1).strip()
            if value.lower() != "not found":
                result["additional_info"][key] = value
                
    # Look for research specializations
    if "PhD" in response_text and "LLM" in response_text:
        result["additional_info"]["research_focus"] = "LLM related research"
        
    # Extract research areas from bullet points
    research_areas = re.findall(r'•\s*([^•\n]+)', response_text)
    if research_areas and len(research_areas) > 0:
        result["additional_info"]["research_areas"] = research_areas
            
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
        INSERT INTO jobs (
            job_title, company_name, required_skills, publication_date, author, url,
            experience_required, salary_range, location, additional_info
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING id
        ''', (
            data.get('job_title', 'not found'),
            data.get('company_name', 'not found'),
            data.get('required_skills', 'not found'),
            data.get('publication_date', 'not found'),
            data.get('author', 'not found'),
            data.get('url', ''),
            data.get('experience_required', 'not found'),
            data.get('salary_range', 'not found'),
            data.get('location', 'not found'),
            json.dumps(data.get('additional_info', {}))
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