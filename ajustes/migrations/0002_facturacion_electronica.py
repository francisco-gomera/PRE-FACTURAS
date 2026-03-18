from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("ajustes", "0001_initial"),
    ]

    operations = [
        migrations.CreateModel(
            name="FacturacionElectronicaConfig",
            fields=[
                ("id_config", models.AutoField(db_column="ID_CONFIG", primary_key=True, serialize=False)),
                ("habilitado", models.BooleanField(db_column="HABILITADO", default=False)),
                ("ambiente", models.CharField(db_column="AMBIENTE", default="precertificacion", max_length=20)),
                ("modo_envio", models.CharField(db_column="MODO_ENVIO", default="manual", max_length=20)),
                ("certificado_ruta", models.CharField(blank=True, db_column="CERTIFICADO_RUTA", max_length=255, null=True)),
                ("certificado_clave", models.CharField(blank=True, db_column="CERTIFICADO_CLAVE", max_length=255, null=True)),
                ("url_recepcion_emisor", models.CharField(blank=True, db_column="URL_RECEPCION_EMISOR", max_length=255, null=True)),
                ("url_aprobacion_emisor", models.CharField(blank=True, db_column="URL_APROBACION_EMISOR", max_length=255, null=True)),
                ("token_activo", models.TextField(blank=True, db_column="TOKEN_ACTIVO", null=True)),
                ("token_expira_en", models.DateTimeField(blank=True, db_column="TOKEN_EXPIRA_EN", null=True)),
                ("observaciones", models.TextField(blank=True, db_column="OBSERVACIONES", null=True)),
                ("creado_en", models.DateTimeField(auto_now_add=True, db_column="CREADO_EN")),
                ("actualizado_en", models.DateTimeField(auto_now=True, db_column="ACTUALIZADO_EN")),
            ],
            options={"db_table": "DGII_ECF_CONFIG"},
        ),
        migrations.CreateModel(
            name="FacturacionElectronicaDocumento",
            fields=[
                ("id_documento", models.AutoField(db_column="ID_DOCUMENTO", primary_key=True, serialize=False)),
                ("id_doc", models.BigIntegerField(db_column="ID_DOC", unique=True)),
                ("tipo_ecf", models.CharField(blank=True, db_column="TIPO_ECF", max_length=2, null=True)),
                ("encf", models.CharField(blank=True, db_column="ENCF", max_length=20, null=True)),
                ("estado", models.CharField(db_column="ESTADO", default="PENDIENTE", max_length=30)),
                ("track_id", models.CharField(blank=True, db_column="TRACK_ID", max_length=120, null=True)),
                ("codigo_seguridad", models.CharField(blank=True, db_column="CODIGO_SEGURIDAD", max_length=20, null=True)),
                ("cliente_rnc", models.CharField(blank=True, db_column="CLIENTE_RNC", max_length=20, null=True)),
                ("cliente_nombre", models.CharField(blank=True, db_column="CLIENTE_NOMBRE", max_length=150, null=True)),
                ("fecha_doc", models.DateTimeField(blank=True, db_column="FECHA_DOC", null=True)),
                ("monto_total", models.DecimalField(db_column="MONTO_TOTAL", decimal_places=4, default=0, max_digits=19)),
                ("xml_generado", models.BooleanField(db_column="XML_GENERADO", default=False)),
                ("firmado", models.BooleanField(db_column="FIRMADO", default=False)),
                ("enviado_dgii", models.BooleanField(db_column="ENVIADO_DGII", default=False)),
                ("respuesta_dgii", models.TextField(blank=True, db_column="RESPUESTA_DGII", null=True)),
                ("url_consulta_qr", models.CharField(blank=True, db_column="URL_CONSULTA_QR", max_length=500, null=True)),
                ("observaciones", models.TextField(blank=True, db_column="OBSERVACIONES", null=True)),
                ("creado_en", models.DateTimeField(auto_now_add=True, db_column="CREADO_EN")),
                ("actualizado_en", models.DateTimeField(auto_now=True, db_column="ACTUALIZADO_EN")),
            ],
            options={
                "db_table": "DGII_ECF_DOCUMENTO",
                "ordering": ["-fecha_doc", "-id_doc"],
            },
        ),
        migrations.CreateModel(
            name="FacturacionElectronicaSecuencia",
            fields=[
                ("id_secuencia", models.AutoField(db_column="ID_SECUENCIA", primary_key=True, serialize=False)),
                ("tipo_ecf", models.CharField(db_column="TIPO_ECF", max_length=2, unique=True)),
                ("descripcion", models.CharField(db_column="DESCRIPCION", max_length=120)),
                ("habilitada", models.BooleanField(db_column="HABILITADA", default=False)),
                ("secuencia_actual", models.BigIntegerField(db_column="SECUENCIA_ACTUAL", default=1)),
                ("secuencia_desde", models.BigIntegerField(db_column="SECUENCIA_DESDE", default=1)),
                ("secuencia_hasta", models.BigIntegerField(db_column="SECUENCIA_HASTA", default=0)),
                ("vencimiento_secuencia", models.DateField(blank=True, db_column="VENCIMIENTO_SECUENCIA", null=True)),
                ("creado_en", models.DateTimeField(auto_now_add=True, db_column="CREADO_EN")),
                ("actualizado_en", models.DateTimeField(auto_now=True, db_column="ACTUALIZADO_EN")),
            ],
            options={
                "db_table": "DGII_ECF_SECUENCIA",
                "ordering": ["tipo_ecf"],
            },
        ),
        migrations.CreateModel(
            name="FacturacionElectronicaEvento",
            fields=[
                ("id_evento", models.AutoField(db_column="ID_EVENTO", primary_key=True, serialize=False)),
                ("tipo_evento", models.CharField(db_column="TIPO_EVENTO", max_length=50)),
                ("detalle", models.TextField(blank=True, db_column="DETALLE", null=True)),
                ("creado_en", models.DateTimeField(auto_now_add=True, db_column="CREADO_EN")),
                (
                    "documento",
                    models.ForeignKey(
                        db_column="ID_DOCUMENTO",
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="eventos",
                        to="ajustes.facturacionelectronicadocumento",
                    ),
                ),
            ],
            options={
                "db_table": "DGII_ECF_EVENTO",
                "ordering": ["-creado_en", "-id_evento"],
            },
        ),
    ]
