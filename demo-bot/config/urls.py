from django.contrib import admin
from django.contrib.admin.views.decorators import staff_member_required
from django.urls import include, path, re_path
from django.conf import settings
from django.views.generic import RedirectView
from django.views.static import serve

admin.site.site_header = "Сервисная папка — диспетчерская"
admin.site.site_title = "Сервисная папка"
admin.site.index_title = "Заявки, объекты и оборудование"
admin.site.site_url = None  # отдельного публичного сайта нет

urlpatterns = [
    path("", RedirectView.as_view(url="/admin/", permanent=False)),
    path("admin/", admin.site.urls),
    path("", include("service.urls")),
    # Фото заявок — только для сотрудников (в проде отдавать через nginx + X-Accel)
    re_path(
        r"^media/(?P<path>.*)$",
        staff_member_required(serve),
        {"document_root": settings.MEDIA_ROOT},
    ),
]
