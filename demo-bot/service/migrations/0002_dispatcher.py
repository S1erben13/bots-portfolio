import django.db.models.deletion
import service.models
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("service", "0001_initial"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="Dispatcher",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("messenger", models.CharField("мессенджер", max_length=20, choices=[("telegram", "Telegram"), ("max", "MAX"), ("console", "Демо (консоль)")])),
                ("chat_id", models.CharField("ID диалога", max_length=64, blank=True)),
                ("is_active", models.BooleanField("активен", default=True)),
                ("link_code", models.CharField("код привязки", max_length=8, unique=True, null=True, blank=True, default=service.models.generate_link_code)),
                ("link_code_expires_at", models.DateTimeField("код действует до", default=service.models.link_code_deadline)),
                ("company", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="dispatchers", to="service.company")),
                ("user", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, to=settings.AUTH_USER_MODEL)),
            ],
            options={"verbose_name": "диспетчер", "verbose_name_plural": "диспетчеры",
                     "constraints": [models.UniqueConstraint(fields=["messenger", "chat_id"], condition=~models.Q(chat_id=""), name="uniq_dispatcher_chat")]},
        ),
        migrations.AlterField(model_name="notification", name="client",
            field=models.ForeignKey("service.client", verbose_name="клиент", on_delete=django.db.models.deletion.CASCADE, related_name="notifications", null=True, blank=True)),
        migrations.AddField(model_name="notification", name="dispatcher",
            field=models.ForeignKey("service.dispatcher", on_delete=django.db.models.deletion.CASCADE, related_name="notifications", null=True, blank=True)),
        migrations.AddField(model_name="notification", name="buttons", field=models.JSONField(default=list, blank=True)),
        migrations.AddConstraint(model_name="notification", constraint=models.CheckConstraint(
            condition=(models.Q(client__isnull=False, dispatcher__isnull=True) | models.Q(client__isnull=True, dispatcher__isnull=False)),
            name="notification_one_recipient")),
    ]
