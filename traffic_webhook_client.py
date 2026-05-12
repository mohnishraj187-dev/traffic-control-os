import requests


def send_webhook(url, payload, timeout=5):
    try:
        resp = requests.post(url, json=payload, timeout=timeout)
        return resp.status_code, resp.text
    except Exception as e:
        return None, str(e)
