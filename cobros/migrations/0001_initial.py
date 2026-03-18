from django.db import migrations, models


class Migration(migrations.Migration):

    initial = True

    dependencies = []

    operations = [
        migrations.CreateModel(
            name="CobroAcuerdo",
            fields=[
                ("id_acuerdo", models.AutoField(db_column="ID_ACUERDO", primary_key=True, serialize=False)),
                ("id_sn", models.CharField(db_column="ID_SN", max_length=20)),
                ("cliente_nombre", models.CharField(db_column="CLIENTE_NOMBRE", max_length=200)),
                ("telefono", models.CharField(blank=True, db_column="TELEFONO", default="", max_length=50)),
                ("sector", models.CharField(blank=True, db_column="SECTOR", default="", max_length=120)),
                ("tipo", models.CharField(db_column="TIPO", default="PROMESA_PAGO", max_length=30)),
                ("fecha_compromiso", models.DateField(blank=True, db_column="FECHA_COMPROMISO", null=True)),
                (
                    "monto_compromiso",
                    models.DecimalField(
                        blank=True,
                        db_column="MONTO_COMPROMISO",
                        decimal_places=2,
                        max_digits=19,
                        null=True,
                    ),
                ),
                ("nota", models.TextField(db_column="NOTA")),
                ("estado", models.CharField(db_column="ESTADO", default="PENDIENTE", max_length=20)),
                ("creado_por_id", models.BigIntegerField(db_column="CREADO_POR_ID")),
                ("fecha_creacion", models.DateTimeField(auto_now_add=True, db_column="FECHA_CREACION")),
                ("fecha_modificacion", models.DateTimeField(auto_now=True, db_column="FECHA_MODIFICACION")),
            ],
            options={
                "db_table": "COBRO_ACUERDO",
                "ordering": ["estado", "-fecha_compromiso", "-fecha_creacion"],
            },
        ),
    ]
