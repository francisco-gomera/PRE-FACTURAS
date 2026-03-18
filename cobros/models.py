from django.db import models


class CobroAcuerdo(models.Model):
    id_acuerdo = models.AutoField(db_column="ID_ACUERDO", primary_key=True)
    id_sn = models.CharField(db_column="ID_SN", max_length=20)
    cliente_nombre = models.CharField(db_column="CLIENTE_NOMBRE", max_length=200)
    telefono = models.CharField(db_column="TELEFONO", max_length=50, blank=True, default="")
    sector = models.CharField(db_column="SECTOR", max_length=120, blank=True, default="")
    tipo = models.CharField(db_column="TIPO", max_length=30, default="PROMESA_PAGO")
    fecha_compromiso = models.DateField(db_column="FECHA_COMPROMISO", null=True, blank=True)
    monto_compromiso = models.DecimalField(
        db_column="MONTO_COMPROMISO",
        max_digits=19,
        decimal_places=2,
        null=True,
        blank=True,
    )
    nota = models.TextField(db_column="NOTA")
    estado = models.CharField(db_column="ESTADO", max_length=20, default="PENDIENTE")
    creado_por_id = models.BigIntegerField(db_column="CREADO_POR_ID")
    fecha_creacion = models.DateTimeField(db_column="FECHA_CREACION", auto_now_add=True)
    fecha_modificacion = models.DateTimeField(db_column="FECHA_MODIFICACION", auto_now=True)

    class Meta:
        db_table = "COBRO_ACUERDO"
        ordering = ["estado", "-fecha_compromiso", "-fecha_creacion"]
