import requests
import json

def send_json_item(url, json_item):
    try:
        # Sending a POST request to the specified URL with the JSON item
        response = requests.post(url, json=json_item)
        
        # Checking if the request was successful
        if response.status_code == 200:
            print("JSON item successfully sent.")
            print("Response:", response.json())
        else:
            print(f"Failed to send JSON item. Status code: {response.status_code}")
            print("Response:", response.text)
    
    except requests.exceptions.RequestException as e:
        print(f"An error occurred: {e}")

# Example usage
url = "http://localhost:8000/"
json_item = {
    "created_at": "2024-08-01T12:34:56.123456Z",
    "content": "some_content",
    "domain": "twitter.com",
    "url": "https://twitter.com/foo"
}

send_json_item(url, json_item)

