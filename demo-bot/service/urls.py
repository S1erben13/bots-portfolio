from django.urls import path

from . import views

app_name = "service"

urlpatterns = [
    path("q/<str:code>/", views.landing, name="landing"),
    path("stickers/equipment/<int:pk>.png", views.equipment_sticker_png, name="sticker_png"),
    path("stickers/site/<int:pk>.pdf", views.site_stickers_pdf, name="site_stickers_pdf"),
]
