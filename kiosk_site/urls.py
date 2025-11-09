# kiosk_site/urls.py
from django.contrib import admin
from django.urls import path
from django.conf import settings
from django.conf.urls.static import static

from assistant import views
from assistant.views import index, ask, ping

urlpatterns = [
    path("admin/", admin.site.urls),
    path("", views.index, name="index"),
    path("ask/", views.ask, name="ask"),
    path("ping/", ping, name="ping"),
]

# Servește STATIC și MEDIA doar în development
if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
    urlpatterns += static(settings.STATIC_URL, document_root=settings.STATIC_ROOT)
