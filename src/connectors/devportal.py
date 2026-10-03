from .filesystem import LocalDirectoryConnector


class DevPortalConnector(LocalDirectoryConnector):
    def __init__(self, path="data/documents/devportal", **kwargs):
        super().__init__(path, source="devportal", **kwargs)
