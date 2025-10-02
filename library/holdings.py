from openalgo import api
import os
from dotenv import load_dotenv

load_dotenv()

client = api(
    api_key=os.getenv("API_KEY"),
    host=os.getenv("HOST_SERVER"),
    ws_url=os.getenv("WEBSOCKET_URL")
)

resp = client.holdings()
print(resp)
