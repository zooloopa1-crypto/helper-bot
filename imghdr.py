import mimetypes

def what(file, h=None):
    """Simple replacement for the removed imghdr.what()"""
    kind = mimetypes.guess_type(file)[0]
    if kind and kind.startswith('image/'):
        return kind.split('/')[-1]
    return None
