import os
import datetime
import requests
from flask import Flask, render_template, request, redirect, url_for, session, jsonify, flash
from flask_sqlalchemy import SQLAlchemy
from apscheduler.schedulers.background import BackgroundScheduler
import logging

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev_key_very_secret")

# Database Configuration
# Use SQLite for simplicity, file-based.
app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get('DATABASE_URL', 'sqlite:///scheduler.db')
# Fix for Render/Heroku Postgres URLs starting with postgres:// instead of postgresql://
if app.config['SQLALCHEMY_DATABASE_URI'].startswith("postgres://"):
    app.config['SQLALCHEMY_DATABASE_URI'] = app.config['SQLALCHEMY_DATABASE_URI'].replace("postgres://", "postgresql://", 1)

app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db = SQLAlchemy(app)

# Facebook Config
FB_APP_ID = os.environ.get("FB_APP_ID")
FB_APP_SECRET = os.environ.get("FB_APP_SECRET")
# This URL must match what is in the Facebook App settings
FB_REDIRECT_URI = os.environ.get("FB_REDIRECT_URI", "https://your-app-url.onrender.com/callback") 

# --- Models ---
class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    fb_user_id = db.Column(db.String(100), unique=True, nullable=False)
    access_token = db.Column(db.String(500), nullable=False)
    name = db.Column(db.String(100))

class ScheduledPost(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    page_id = db.Column(db.String(100), nullable=False)
    page_name = db.Column(db.String(200))
    message = db.Column(db.Text, nullable=False)
    image_url = db.Column(db.String(500), nullable=True) # Optional image
    scheduled_time = db.Column(db.DateTime, nullable=False)
    status = db.Column(db.String(20), default='pending') # pending, published, failed
    error_msg = db.Column(db.Text, nullable=True)

# --- Routes ---

@app.route('/')
def index():
    if 'user_id' in session:
        return redirect(url_for('dashboard'))
    return render_template('login.html', app_id=FB_APP_ID, redirect_uri=FB_REDIRECT_URI)

@app.route('/callback')
def callback():
    code = request.args.get('code')
    if not code:
        return "Error: No code provided from Facebook", 400
    
    # Exchange code for access token
    token_url = f"https://graph.facebook.com/v18.0/oauth/access_token?client_id={FB_APP_ID}&redirect_uri={FB_REDIRECT_URI}&client_secret={FB_APP_SECRET}&code={code}"
    resp = requests.get(token_url).json()
    
    if 'access_token' not in resp:
        return f"Error getting token: {resp}", 400
    
    short_lived_token = resp['access_token']

    # Exchange for Long-Lived Token (60 days)
    exchange_url = f"https://graph.facebook.com/v18.0/oauth/access_token?grant_type=fb_exchange_token&client_id={FB_APP_ID}&client_secret={FB_APP_SECRET}&fb_exchange_token={short_lived_token}"
    try:
        exchange_resp = requests.get(exchange_url).json()
        access_token = exchange_resp.get('access_token', short_lived_token) # Fallback if fails
    except Exception as e:
        logger.error(f"Failed to exchange token: {e}")
        access_token = short_lived_token
    
    # Get User Info
    me_url = f"https://graph.facebook.com/me?access_token={access_token}"
    user_data = requests.get(me_url).json()
    fb_id = user_data.get('id')
    name = user_data.get('name')
    
    # Save/Update User
    user = User.query.filter_by(fb_user_id=fb_id).first()
    if not user:
        user = User(fb_user_id=fb_id, access_token=access_token, name=name)
        db.session.add(user)
    else:
        user.access_token = access_token
    
    db.session.commit()
    session['user_id'] = user.id
    
    return redirect(url_for('dashboard'))

@app.route('/dashboard')
def dashboard():
    if 'user_id' not in session:
        return redirect(url_for('index'))
    
    user = db.session.get(User, session['user_id'])
    
    # Fetch Pages managed by user
    pages_url = f"https://graph.facebook.com/me/accounts?access_token={user.access_token}"
    pages_resp = requests.get(pages_url).json()
    pages = pages_resp.get('data', [])
    
    # Fetch upcoming scheduled posts
    posts = ScheduledPost.query.filter_by(user_id=user.id).order_by(ScheduledPost.scheduled_time).all()
    
    return render_template('dashboard.html', user=user, pages=pages, posts=posts)

@app.route('/schedule', methods=['POST'])
def schedule():
    if 'user_id' not in session:
        return redirect(url_for('index'))
    
    user = db.session.get(User, session['user_id'])
    
    page_id = request.form.get('page_id')
    # We need to store the page name for display, usually we'd get it from the form or re-fetch
    # For simplicity, let's just assume we passed it or handle it simply. 
    # Let's hack getting the name from the request logic if possible or just store ID for now.
    page_name = request.form.get('page_name', 'Unknown Page') 
    
    message = request.form.get('message')
    image_url = request.form.get('image_url')
    date_str = request.form.get('date') # YYYY-MM-DD
    time_str = request.form.get('time') # HH:MM
    
    if not (page_id and message and date_str and time_str):
        flash("All fields are required")
        return redirect(url_for('dashboard'))
        
    scheduled_dt = datetime.datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M")
    
    new_post = ScheduledPost(
        user_id=user.id,
        page_id=page_id,
        page_name=page_name,
        message=message,
        image_url=image_url,
        scheduled_time=scheduled_dt
    )
    db.session.add(new_post)
    db.session.commit()
    
    flash("Post scheduled successfully!")
    return redirect(url_for('dashboard'))

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('index'))

# --- Scheduler Logic ---

def publish_post(post_id):
    """
    Function to actually publish the post to Facebook.
    """
    with app.app_context():
        post = db.session.get(ScheduledPost, post_id)
        if not post or post.status != 'pending':
            return

        user = db.session.get(User, post.user_id)
        
        # 1. Get Page Access Token
        # We need to query /me/accounts again or store the page token. 
        # Storing page tokens is better, but they can expire. 
        # For simplicity, we fetch it fresh.
        accounts_url = f"https://graph.facebook.com/me/accounts?access_token={user.access_token}"
        resp = requests.get(accounts_url).json()
        
        page_access_token = None
        for page in resp.get('data', []):
            if page['id'] == post.page_id:
                page_access_token = page['access_token']
                break
        
        if not page_access_token:
            post.status = 'failed'
            post.error_msg = "Could not retrieve Page Access Token"
            db.session.commit()
            return

        # 2. Publish
        # If we have an image, we use the photos endpoint, otherwise feed
        if post.image_url:
            post_url = f"https://graph.facebook.com/{post.page_id}/photos"
            payload = {
                'url': post.image_url,
                'caption': post.message,
                'access_token': page_access_token
            }
        else:
            post_url = f"https://graph.facebook.com/{post.page_id}/feed"
            payload = {
                'message': post.message,
                'access_token': page_access_token
            }
        
        try:
            r = requests.post(post_url, data=payload)
            r.raise_for_status()
            logger.info(f"Published post {post.id} to page {post.page_id}")
            post.status = 'published'
        except Exception as e:
            logger.error(f"Failed to publish post {post.id}: {e}")
            # If it's a JSON response with error
            try:
                err_resp = r.json()
                post.error_msg = str(err_resp.get('error', {}).get('message', str(e)))
            except:
                post.error_msg = str(e)
            post.status = 'failed'
            
        db.session.commit()

def check_for_posts():
    """
    Runs every minute. Checks for posts that are due.
    """
    with app.app_context():
        now = datetime.datetime.now()
        # Find pending posts where time <= now
        pending_posts = ScheduledPost.query.filter(
            ScheduledPost.status == 'pending',
            ScheduledPost.scheduled_time <= now
        ).all()
        
    # Throttling: Publish only 1 post per minute to avoid spam detection
    if pending_posts:
        post = pending_posts[0]
        logger.info(f"Throttling: Publishing 1 of {len(pending_posts)} pending posts")
        publish_post(post.id)

# Initialize Scheduler
scheduler = BackgroundScheduler()
scheduler.add_job(func=check_for_posts, trigger="interval", seconds=60)
scheduler.start()

# Initialize DB
with app.app_context():
    db.create_all()

if __name__ == '__main__':
    app.run(debug=True, port=5000)
4. Archivo: templates/login.html
Truco: Para crear la carpeta, en el nombre del archivo escribe templates/login.html.

<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <title>Login - Social Scheduler</title>
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
    <style>
        body { display: flex; align-items: center; justify-content: center; height: 100vh; background-color: #f0f2f5; }
        .card { width: 100%; max-width: 400px; padding: 20px; }
    </style>
</head>
<body>
    <div class="card shadow">
        <h3 class="text-center mb-4">Social Scheduler</h3>
        <p class="text-muted text-center">Personal Facebook Manager</p>
        
        <a href="https://www.facebook.com/v18.0/dialog/oauth?client_id={{ app_id }}&redirect_uri={{ redirect_uri }}&scope=pages_manage_posts,pages_read_engagement,pages_show_list" class="btn btn-primary w-100">
            Login with Facebook
        </a>
        <div class="mt-3 text-center text-small">
            <small>Ensure your App is in Development Mode and you are an Admin.</small>
        </div>
    </div>
</body>
</html>
