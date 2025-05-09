from django.contrib.gis.db import models as gis_models

class FuelStation(gis_models.Model):
    name = gis_models.CharField(max_length=255)
    address = gis_models.CharField(max_length=255)
    city = gis_models.CharField(max_length=100)
    state = gis_models.CharField(max_length=100)
    price = gis_models.FloatField()
    location = gis_models.PointField(geography=True)

    def __str__(self):
        return f"{self.name} - {self.city}, {self.state}"
