from django.contrib.admin.views.decorators import staff_member_required
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, render

from . import links, qr
from .models import Equipment, Site
from .services import EquipmentNotFound, find_equipment


def landing(request, code: str):
    """Страница, на которую ведёт QR: показывает оборудование и кнопки мессенджеров."""
    try:
        equipment = find_equipment(code)
    except EquipmentNotFound:
        raise Http404("Код не найден")
    return render(
        request,
        "service/landing.html",
        {
            "equipment": equipment,
            "telegram_url": links.telegram_link(equipment.code),
            "max_url": links.max_link(equipment.code),
        },
    )


@staff_member_required
def equipment_sticker_png(request, pk: int):
    equipment = get_object_or_404(Equipment.objects.select_related("site__company"), pk=pk)
    response = HttpResponse(qr.sticker_png(equipment), content_type="image/png")
    disposition = "attachment" if request.GET.get("download") else "inline"
    response["Content-Disposition"] = f'{disposition}; filename="qr-{equipment.code}.png"'
    return response


@staff_member_required
def site_stickers_pdf(request, pk: int):
    site = get_object_or_404(Site.objects.select_related("company"), pk=pk)
    items = site.equipment.filter(is_active=True).select_related("site__company")
    response = HttpResponse(qr.stickers_pdf(items, title=site.name), content_type="application/pdf")
    response["Content-Disposition"] = f'inline; filename="qr-site-{site.pk}.pdf"'
    return response
