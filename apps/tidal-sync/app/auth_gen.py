import tidalapi
import json

def generate_session():
    print("Initializing Tidal OAuth Flow...")
    session = tidalapi.Session()
    
    # This will print a URL to your terminal. 
    # Click it, log in to Tidal in your browser, and authorize the app.
    session.login_oauth_simple()
    
    if session.check_login():
        print(f"Successfully logged in as: {session.user.id}")
        
        # Package the tokens we need for headless login
        data = {
            'token_type': session.token_type,
            'access_token': session.access_token,
            'refresh_token': session.refresh_token,
            # We save expiry just for our own reference
            'expiry_time': session.expiry_time.isoformat() if session.expiry_time else None 
        }
        
        with open('session.json', 'w') as f:
            json.dump(data, f, indent=4)
            
        print("Success! 'session.json' created. Keep this file secure.")
    else:
        print("Login failed.")

if __name__ == "__main__":
    generate_session()