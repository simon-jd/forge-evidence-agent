import json
import hashlib

class RequestCache:
    def __init__(self):
        self.saved = {}

    def submit(self, key, body, create):
        digest = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()
        if key in self.saved:
            return self.saved[key][1]
        response = create(body)
        self.saved[key] = (digest, response)
        return response
