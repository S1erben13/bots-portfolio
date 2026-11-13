from django.db import models

from service.models import Client, Equipment


class Conversation(models.Model):
    """Состояние диалога с клиентом (на каком шаге сценария он находится)."""

    class State(models.TextChoices):
        IDLE = "idle", "Ожидание"
        AWAIT_CODE = "await_code", "Ждём код оборудования"
        AWAIT_DESCRIPTION = "await_description", "Ждём описание"
        AWAIT_PHOTO = "await_photo", "Ждём фото"

    client = models.OneToOneField(
        Client, verbose_name="клиент", on_delete=models.CASCADE, related_name="conversation"
    )
    state = models.CharField("шаг", max_length=30, choices=State.choices, default=State.IDLE)
    equipment = models.ForeignKey(
        Equipment, verbose_name="оборудование", on_delete=models.SET_NULL, null=True, blank=True
    )
    draft_description = models.TextField("черновик описания", blank=True)
    updated_at = models.DateTimeField("обновлено", auto_now=True)

    class Meta:
        verbose_name = "диалог"
        verbose_name_plural = "диалоги"

    def __str__(self):
        return f"{self.client}: {self.get_state_display()}"

    def reset(self):
        self.state = self.State.IDLE
        self.equipment = None
        self.draft_description = ""
