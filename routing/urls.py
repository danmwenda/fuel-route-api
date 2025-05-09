from django.urls import path
from .views import OptimizedRouteView

urlpatterns = [
    path('route', OptimizedRouteView.as_view(), name='optimized_route')
]