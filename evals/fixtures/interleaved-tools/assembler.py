import json

class ToolAssembler:
    def __init__(self):
        self.buffers = {}

    def append(self, call_id, fragment):
        self.buffers["active"] = self.buffers.get("active", "") + fragment

    def finish(self, call_id):
        return json.loads(self.buffers.pop("active"))
