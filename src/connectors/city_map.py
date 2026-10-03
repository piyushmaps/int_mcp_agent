from .filesystem import LocalDirectoryConnector


class CityMapConnector(LocalDirectoryConnector):
    def __init__(self, path="data/documents/city_map", **kwargs):
        super().__init__(path, source="city_map", **kwargs)
