import codecs

class TextDecoder:
    def __init__(self):
        self.codec = codecs.getincrementaldecoder("utf-8")()

    def feed(self, data, final=False):
        return data.decode("utf-8", errors="replace")
