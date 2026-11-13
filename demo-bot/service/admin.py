"""Админка диспетчера.

Статус заявки меняется только через services.change_status — так пишется история
и клиенту уходит уведомление, откуда бы ни пришло изменение (форма или массовое действие).
"""

from django import forms
from django.contrib import admin, messages
from django.db import transaction
from django.db.models import Count, Q
from django.http import HttpResponse
from django.urls import reverse
from django.utils.html import format_html

from . import qr, services
from .links import qr_link
from .models import (
    Client,
    Company,
    Equipment,
    Notification,
    Site,
    Ticket,
    TicketPhoto,
    TicketStatusChange,
)

STATUS_COLORS = {
    Ticket.Status.NEW: "#d93025",
    Ticket.Status.ACCEPTED: "#e8a100",
    Ticket.Status.EN_ROUTE: "#1a73e8",
    Ticket.Status.DONE: "#188038",
    Ticket.Status.CANCELLED: "#80868b",
}


def badge(text: str, color: str):
    return format_html(
        '<span style="background:{};color:#fff;padding:2px 8px;border-radius:10px;'
        'white-space:nowrap;font-size:12px">{}</span>',
        color,
        text,
    )


@admin.register(Company)
class CompanyAdmin(admin.ModelAdmin):
    list_display = ["name", "phone", "sites_count"]
    search_fields = ["name"]

    def get_queryset(self, request):
        return super().get_queryset(request).annotate(_sites=Count("sites"))

    @admin.display(description="объектов", ordering="_sites")
    def sites_count(self, obj):
        return obj._sites


class EquipmentInline(admin.TabularInline):
    model = Equipment
    extra = 0
    fields = ["kind", "name", "location", "model_name", "code", "is_active", "sticker"]
    readonly_fields = ["sticker"]
    show_change_link = True

    @admin.display(description="наклейка")
    def sticker(self, obj):
        if not obj.pk:
            return "—"
        return format_html(
            '<a href="{}" target="_blank">PNG</a>', reverse("service:sticker_png", args=[obj.pk])
        )


@admin.register(Site)
class SiteAdmin(admin.ModelAdmin):
    list_display = ["name", "address", "company", "equipment_count", "open_tickets", "stickers"]
    list_filter = ["company"]
    search_fields = ["name", "address"]
    inlines = [EquipmentInline]

    def get_queryset(self, request):
        return (
            super()
            .get_queryset(request)
            .select_related("company")
            .annotate(
                _equipment=Count("equipment", distinct=True),
                _open=Count(
                    "tickets",
                    filter=Q(tickets__status__in=Ticket.OPEN_STATUSES),
                    distinct=True,
                ),
            )
        )

    @admin.display(description="оборудования", ordering="_equipment")
    def equipment_count(self, obj):
        return obj._equipment

    @admin.display(description="открытых заявок", ordering="_open")
    def open_tickets(self, obj):
        return obj._open

    @admin.display(description="QR-наклейки")
    def stickers(self, obj):
        return format_html(
            '<a href="{}" target="_blank">PDF для печати</a>',
            reverse("service:site_stickers_pdf", args=[obj.pk]),
        )


@admin.register(Equipment)
class EquipmentAdmin(admin.ModelAdmin):
    list_display = ["name", "kind", "site", "code", "is_active", "sticker_link"]
    list_filter = ["kind", "is_active", "site__company", "site"]
    search_fields = ["name", "code", "location", "site__name"]
    list_select_related = ["site"]
    readonly_fields = ["qr_target", "sticker_preview"]
    actions = ["download_stickers"]
    fieldsets = [
        (None, {"fields": ["site", "kind", "name", "location", "model_name", "is_active"]}),
        ("QR-наклейка", {"fields": ["code", "qr_target", "sticker_preview"]}),
    ]

    @admin.display(description="наклейка")
    def sticker_link(self, obj):
        return format_html(
            '<a href="{}" target="_blank">PNG</a>', reverse("service:sticker_png", args=[obj.pk])
        )

    @admin.display(description="ссылка в QR")
    def qr_target(self, obj):
        if not obj.pk:
            return "появится после сохранения"
        url = qr_link(obj)
        return format_html('<a href="{}" target="_blank">{}</a>', url, url)

    @admin.display(description="превью")
    def sticker_preview(self, obj):
        if not obj.pk:
            return "—"
        url = reverse("service:sticker_png", args=[obj.pk])
        return format_html(
            '<a href="{}?download=1"><img src="{}" style="width:220px;border:1px solid #ddd"></a>'
            "<br>Нажмите, чтобы скачать PNG",
            url,
            url,
        )

    @admin.action(description="Скачать наклейки выбранного оборудования (PDF)")
    def download_stickers(self, request, queryset):
        pdf = qr.stickers_pdf(queryset.select_related("site__company"))
        response = HttpResponse(pdf, content_type="application/pdf")
        response["Content-Disposition"] = 'attachment; filename="qr-stickers.pdf"'
        return response


@admin.register(Client)
class ClientAdmin(admin.ModelAdmin):
    list_display = ["__str__", "messenger", "username", "phone", "tickets_count", "created_at"]
    list_filter = ["messenger"]
    search_fields = ["name", "username", "phone", "external_id"]
    readonly_fields = ["messenger", "external_id", "created_at"]

    def get_queryset(self, request):
        return super().get_queryset(request).annotate(_tickets=Count("tickets"))

    @admin.display(description="заявок", ordering="_tickets")
    def tickets_count(self, obj):
        return obj._tickets


class TicketPhotoInline(admin.TabularInline):
    model = TicketPhoto
    extra = 0
    fields = ["preview", "image", "created_at"]
    readonly_fields = ["preview", "created_at"]

    @admin.display(description="превью")
    def preview(self, obj):
        if not obj.image:
            return "—"
        return format_html(
            '<a href="{}" target="_blank"><img src="{}" style="max-height:120px"></a>',
            obj.image.url,
            obj.image.url,
        )


class StatusHistoryInline(admin.TabularInline):
    model = TicketStatusChange
    extra = 0
    can_delete = False
    fields = ["created_at", "old_status", "new_status", "changed_by", "comment"]
    readonly_fields = fields

    def has_add_permission(self, request, obj=None):
        return False


class NotificationInline(admin.TabularInline):
    model = Notification
    extra = 0
    can_delete = False
    fields = ["created_at", "text", "state", "attempts", "sent_at"]
    readonly_fields = fields
    verbose_name_plural = "уведомления клиенту"

    def has_add_permission(self, request, obj=None):
        return False


class TicketForm(forms.ModelForm):
    status_comment = forms.CharField(
        label="комментарий клиенту",
        required=False,
        max_length=500,
        help_text="Уйдёт клиенту вместе с уведомлением о смене статуса.",
    )

    # Статус на момент открытия формы: отличаем «диспетчер выбрал новый статус»
    # от «форма устарела, пока её держали открытой».
    status_seen = forms.CharField(widget=forms.HiddenInput, required=False)

    class Meta:
        model = Ticket
        fields = "__all__"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance.pk:
            self.fields["status_seen"].initial = self.instance.status

    def clean(self):
        cleaned = super().clean()
        equipment, site = cleaned.get("equipment"), cleaned.get("site")
        if equipment and site and equipment.site_id != site.pk:
            self.add_error("equipment", "Оборудование относится к другому объекту.")
        return cleaned


@admin.register(Ticket)
class TicketAdmin(admin.ModelAdmin):
    form = TicketForm
    list_display = [
        "number_display",
        "status_badge",
        "site",
        "equipment",
        "short_description",
        "client",
        "technician",
        "created_at",
    ]
    list_display_links = ["number_display", "short_description"]
    list_filter = ["status", "company", "site", "created_at"]
    search_fields = ["=id", "description", "equipment__code", "client__name", "client__username"]
    list_select_related = ["site", "equipment", "client"]
    date_hierarchy = "created_at"
    autocomplete_fields = ["client"]
    readonly_fields = ["company", "created_at", "updated_at"]
    fieldsets = [
        (None, {"fields": ["status", "status_comment", "technician", "status_seen"]}),
        ("Что случилось", {"fields": ["site", "equipment", "description", "client"]}),
        ("Служебное", {"fields": ["internal_note", "company", "created_at", "updated_at"]}),
    ]
    inlines = [TicketPhotoInline, StatusHistoryInline, NotificationInline]
    actions = ["mark_accepted", "mark_en_route", "mark_done", "mark_cancelled"]

    @admin.display(description="№", ordering="id")
    def number_display(self, obj):
        return obj.number

    @admin.display(description="статус", ordering="status")
    def status_badge(self, obj):
        return badge(obj.get_status_display(), STATUS_COLORS.get(obj.status, "#555"))

    @admin.display(description="описание")
    def short_description(self, obj):
        text = obj.description.replace("\n", " ")
        return text if len(text) <= 60 else text[:57] + "…"

    def save_model(self, request, obj, form, change):
        obj.company = obj.site.company
        if change:
            with transaction.atomic():
                # Статус в БД мог поменять другой диспетчер, пока открыта форма:
                # сохраняем прочие поля поверх актуального статуса, а сам статус
                # меняет только сервис (с историей и уведомлением).
                current = Ticket.objects.select_for_update().get(pk=obj.pk).status
                wanted = obj.status
                seen = form.cleaned_data.get("status_seen") or form.initial.get("status")
                obj.status = current
                super().save_model(request, obj, form, change)
                if wanted != seen and wanted != current:
                    services.change_status(
                        obj, wanted, user=request.user, comment=form.cleaned_data["status_comment"]
                    )
        else:
            super().save_model(request, obj, form, change)
            if not change:
                TicketStatusChange.objects.create(
                    ticket=obj, old_status="", new_status=obj.status, changed_by=request.user
                )

    def _bulk_status(self, request, queryset, status):
        changed = sum(services.change_status(t, status, user=request.user) for t in queryset)
        self.message_user(
            request,
            f"Статус «{Ticket.Status(status).label}» установлен у заявок: {changed}. "
            "Клиентам отправлены уведомления.",
            messages.SUCCESS,
        )

    @admin.action(description="Принять в работу")
    def mark_accepted(self, request, queryset):
        self._bulk_status(request, queryset, Ticket.Status.ACCEPTED)

    @admin.action(description="Техник выехал")
    def mark_en_route(self, request, queryset):
        self._bulk_status(request, queryset, Ticket.Status.EN_ROUTE)

    @admin.action(description="Выполнена")
    def mark_done(self, request, queryset):
        self._bulk_status(request, queryset, Ticket.Status.DONE)

    @admin.action(description="Отменить")
    def mark_cancelled(self, request, queryset):
        self._bulk_status(request, queryset, Ticket.Status.CANCELLED)


@admin.register(Notification)
class NotificationAdmin(admin.ModelAdmin):
    list_display = ["created_at", "client", "ticket", "short_text", "state", "attempts", "sent_at"]
    list_filter = ["state", "client__messenger"]
    readonly_fields = [
        "client",
        "ticket",
        "text",
        "state",
        "attempts",
        "last_error",
        "created_at",
        "sent_at",
    ]
    actions = ["retry"]

    def has_add_permission(self, request):
        return False

    @admin.display(description="текст")
    def short_text(self, obj):
        return obj.text[:80]

    @admin.action(description="Отправить повторно")
    def retry(self, request, queryset):
        count = queryset.exclude(state=Notification.State.SENT).update(
            state=Notification.State.PENDING, attempts=0, last_error="", next_attempt_at=None
        )
        self.message_user(request, f"Поставлено в очередь повторно: {count}")
