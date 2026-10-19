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


class CobroCartaEnviada(models.Model):
    id_carta = models.AutoField(db_column="ID_CARTA", primary_key=True)
    id_sn = models.CharField(db_column="ID_SN", max_length=20)
    cliente_nombre = models.CharField(db_column="CLIENTE_NOMBRE", max_length=200)
    telefono = models.CharField(db_column="TELEFONO", max_length=50, blank=True, default="")
    sector = models.CharField(db_column="SECTOR", max_length=120, blank=True, default="")
    tipo = models.CharField(db_column="TIPO", max_length=30, default="AVISO")
    medio_envio = models.CharField(db_column="MEDIO_ENVIO", max_length=30, default="IMPRESA")
    fecha_envio = models.DateField(db_column="FECHA_ENVIO")
    estado = models.CharField(db_column="ESTADO", max_length=20, default="ENVIADA")
    fecha_seguimiento = models.DateField(db_column="FECHA_SEGUIMIENTO", null=True, blank=True)
    nota = models.TextField(db_column="NOTA")
    creado_por_id = models.BigIntegerField(db_column="CREADO_POR_ID")
    fecha_creacion = models.DateTimeField(db_column="FECHA_CREACION", auto_now_add=True)
    fecha_modificacion = models.DateTimeField(db_column="FECHA_MODIFICACION", auto_now=True)

    class Meta:
        db_table = "COBRO_CARTA_ENVIADA"
        ordering = ["estado", "-fecha_envio", "-fecha_creacion"]


class CobroCobrador(models.Model):
    id_cobrador = models.AutoField(db_column="ID_COBRADOR", primary_key=True)
    nombre = models.CharField(db_column="NOMBRE", max_length=150)
    id_sector = models.DecimalField(db_column="ID_SECTOR", max_digits=18, decimal_places=0, null=True, blank=True)
    sector_nombre = models.CharField(db_column="SECTOR_NOMBRE", max_length=120, blank=True, default="")
    telefono = models.CharField(db_column="TELEFONO", max_length=50, blank=True, default="")
    activo = models.BooleanField(db_column="ACTIVO", default=True)
    creado_por_id = models.BigIntegerField(db_column="CREADO_POR_ID", null=True, blank=True)
    fecha_creacion = models.DateTimeField(db_column="FECHA_CREACION", auto_now_add=True)
    fecha_modificacion = models.DateTimeField(db_column="FECHA_MODIFICACION", auto_now=True)

    class Meta:
        db_table = "COBRO_COBRADOR"
        ordering = ["nombre"]

    def __str__(self):
        return self.nombre


class CobroCobradorCliente(models.Model):
    id_asignacion = models.AutoField(db_column="ID_ASIGNACION", primary_key=True)
    cobrador = models.ForeignKey(
        CobroCobrador,
        on_delete=models.CASCADE,
        related_name="clientes_asignados",
        db_column="ID_COBRADOR",
    )
    id_sn = models.CharField(db_column="ID_SN", max_length=20)
    cliente_nombre = models.CharField(db_column="CLIENTE_NOMBRE", max_length=200, blank=True, default="")
    cedula = models.CharField(db_column="CEDULA", max_length=30, blank=True, default="")
    apodo = models.CharField(db_column="APODO", max_length=100, blank=True, default="")
    sector = models.CharField(db_column="SECTOR", max_length=120, blank=True, default="")
    orden = models.IntegerField(db_column="ORDEN", default=0)
    fecha_asignacion = models.DateTimeField(db_column="FECHA_ASIGNACION", auto_now_add=True)

    class Meta:
        db_table = "COBRO_COBRADOR_CLIENTE"
        unique_together = [("cobrador", "id_sn")]
        ordering = ["orden", "cliente_nombre"]

    def __str__(self):
        return f"{self.cobrador.nombre} - {self.cliente_nombre}"
