from django.db import models


class SegModulo(models.Model):
    codigo = models.CharField(db_column="CODIGO", max_length=50, unique=True)
    nombre = models.CharField(db_column="NOMBRE", max_length=120)
    descripcion = models.CharField(db_column="DESCRIPCION", max_length=255, blank=True, null=True)
    activo = models.BooleanField(db_column="ACTIVO", default=True)
    creado_en = models.DateTimeField(db_column="CREADO_EN", auto_now_add=True)
    actualizado_en = models.DateTimeField(db_column="ACTUALIZADO_EN", auto_now=True)

    class Meta:
        db_table = "SEG_MODULO"


class SegPermiso(models.Model):
    modulo = models.ForeignKey(
        SegModulo,
        db_column="ID_MODULO",
        on_delete=models.PROTECT,
        related_name="permisos",
    )
    codigo = models.CharField(db_column="CODIGO", max_length=80)
    nombre = models.CharField(db_column="NOMBRE", max_length=150)
    descripcion = models.CharField(db_column="DESCRIPCION", max_length=255, blank=True, null=True)
    activo = models.BooleanField(db_column="ACTIVO", default=True)
    creado_en = models.DateTimeField(db_column="CREADO_EN", auto_now_add=True)
    actualizado_en = models.DateTimeField(db_column="ACTUALIZADO_EN", auto_now=True)

    class Meta:
        db_table = "SEG_PERMISO"
        constraints = [
            models.UniqueConstraint(fields=["modulo", "codigo"], name="uq_permiso_modulo_codigo"),
        ]


class SegRol(models.Model):
    codigo = models.CharField(db_column="CODIGO", max_length=50, unique=True)
    nombre = models.CharField(db_column="NOMBRE", max_length=120)
    descripcion = models.CharField(db_column="DESCRIPCION", max_length=255, blank=True, null=True)
    activo = models.BooleanField(db_column="ACTIVO", default=True)
    creado_en = models.DateTimeField(db_column="CREADO_EN", auto_now_add=True)
    actualizado_en = models.DateTimeField(db_column="ACTUALIZADO_EN", auto_now=True)

    class Meta:
        db_table = "SEG_ROL"


class SegRolPermiso(models.Model):
    rol = models.ForeignKey(
        SegRol,
        db_column="ID_ROL",
        on_delete=models.CASCADE,
        related_name="rol_permisos",
    )
    permiso = models.ForeignKey(
        SegPermiso,
        db_column="ID_PERMISO",
        on_delete=models.CASCADE,
        related_name="permiso_roles",
    )

    class Meta:
        db_table = "SEG_ROL_PERMISO"
        constraints = [
            models.UniqueConstraint(fields=["rol", "permiso"], name="uq_rol_permiso"),
        ]


class SegUsuarioRol(models.Model):
    id_usuario = models.BigIntegerField(db_column="ID_USUARIO")
    rol = models.ForeignKey(
        SegRol,
        db_column="ID_ROL",
        on_delete=models.CASCADE,
        related_name="usuario_roles",
    )

    class Meta:
        db_table = "SEG_USUARIO_ROL"
        constraints = [
            models.UniqueConstraint(fields=["id_usuario", "rol"], name="uq_usuario_rol"),
        ]


class SegUsuarioPermiso(models.Model):
    id_usuario = models.BigIntegerField(db_column="ID_USUARIO")
    permiso = models.ForeignKey(
        SegPermiso,
        db_column="ID_PERMISO",
        on_delete=models.CASCADE,
        related_name="usuario_permisos",
    )
    permitido = models.BooleanField(db_column="PERMITIDO", default=True)

    class Meta:
        db_table = "SEG_USUARIO_PERMISO"
        constraints = [
            models.UniqueConstraint(fields=["id_usuario", "permiso"], name="uq_usuario_permiso"),
        ]


class UsuarioFirma(models.Model):
    id_firma = models.AutoField(db_column="ID_FIRMA", primary_key=True)
    id_usuario = models.BigIntegerField(db_column="ID_USUARIO", unique=True)
    firma = models.BinaryField(db_column="FIRMA", blank=True, null=True)
    creado_en = models.DateTimeField(db_column="CREADO_EN", auto_now_add=True)
    actualizado_en = models.DateTimeField(db_column="ACTUALIZADO_EN", auto_now=True)

    class Meta:
        db_table = "SEG_USUARIO_FIRMA"
        ordering = ["id_usuario"]


class FacturacionElectronicaConfig(models.Model):
    id_config = models.AutoField(db_column="ID_CONFIG", primary_key=True)
    habilitado = models.BooleanField(db_column="HABILITADO", default=False)
    ambiente = models.CharField(db_column="AMBIENTE", max_length=20, default="precertificacion")
    modo_envio = models.CharField(db_column="MODO_ENVIO", max_length=20, default="manual")
    certificado_ruta = models.CharField(db_column="CERTIFICADO_RUTA", max_length=255, blank=True, null=True)
    certificado_clave = models.CharField(db_column="CERTIFICADO_CLAVE", max_length=255, blank=True, null=True)
    url_recepcion_emisor = models.CharField(db_column="URL_RECEPCION_EMISOR", max_length=255, blank=True, null=True)
    url_aprobacion_emisor = models.CharField(db_column="URL_APROBACION_EMISOR", max_length=255, blank=True, null=True)
    token_activo = models.TextField(db_column="TOKEN_ACTIVO", blank=True, null=True)
    token_expira_en = models.DateTimeField(db_column="TOKEN_EXPIRA_EN", blank=True, null=True)
    observaciones = models.TextField(db_column="OBSERVACIONES", blank=True, null=True)
    creado_en = models.DateTimeField(db_column="CREADO_EN", auto_now_add=True)
    actualizado_en = models.DateTimeField(db_column="ACTUALIZADO_EN", auto_now=True)

    class Meta:
        db_table = "DGII_ECF_CONFIG"


class FacturacionElectronicaSecuencia(models.Model):
    id_secuencia = models.AutoField(db_column="ID_SECUENCIA", primary_key=True)
    tipo_ecf = models.CharField(db_column="TIPO_ECF", max_length=2, unique=True)
    descripcion = models.CharField(db_column="DESCRIPCION", max_length=120)
    habilitada = models.BooleanField(db_column="HABILITADA", default=False)
    secuencia_actual = models.BigIntegerField(db_column="SECUENCIA_ACTUAL", default=1)
    secuencia_desde = models.BigIntegerField(db_column="SECUENCIA_DESDE", default=1)
    secuencia_hasta = models.BigIntegerField(db_column="SECUENCIA_HASTA", default=0)
    vencimiento_secuencia = models.DateField(db_column="VENCIMIENTO_SECUENCIA", blank=True, null=True)
    creado_en = models.DateTimeField(db_column="CREADO_EN", auto_now_add=True)
    actualizado_en = models.DateTimeField(db_column="ACTUALIZADO_EN", auto_now=True)

    class Meta:
        db_table = "DGII_ECF_SECUENCIA"
        ordering = ["tipo_ecf"]


class FacturacionElectronicaDocumento(models.Model):
    id_documento = models.AutoField(db_column="ID_DOCUMENTO", primary_key=True)
    id_doc = models.BigIntegerField(db_column="ID_DOC", unique=True)
    tipo_ecf = models.CharField(db_column="TIPO_ECF", max_length=2, blank=True, null=True)
    encf = models.CharField(db_column="ENCF", max_length=20, blank=True, null=True)
    estado = models.CharField(db_column="ESTADO", max_length=30, default="PENDIENTE")
    track_id = models.CharField(db_column="TRACK_ID", max_length=120, blank=True, null=True)
    codigo_seguridad = models.CharField(db_column="CODIGO_SEGURIDAD", max_length=20, blank=True, null=True)
    cliente_rnc = models.CharField(db_column="CLIENTE_RNC", max_length=20, blank=True, null=True)
    cliente_nombre = models.CharField(db_column="CLIENTE_NOMBRE", max_length=150, blank=True, null=True)
    fecha_doc = models.DateTimeField(db_column="FECHA_DOC", blank=True, null=True)
    monto_total = models.DecimalField(db_column="MONTO_TOTAL", max_digits=19, decimal_places=4, default=0)
    xml_generado = models.BooleanField(db_column="XML_GENERADO", default=False)
    firmado = models.BooleanField(db_column="FIRMADO", default=False)
    enviado_dgii = models.BooleanField(db_column="ENVIADO_DGII", default=False)
    respuesta_dgii = models.TextField(db_column="RESPUESTA_DGII", blank=True, null=True)
    url_consulta_qr = models.CharField(db_column="URL_CONSULTA_QR", max_length=500, blank=True, null=True)
    observaciones = models.TextField(db_column="OBSERVACIONES", blank=True, null=True)
    creado_en = models.DateTimeField(db_column="CREADO_EN", auto_now_add=True)
    actualizado_en = models.DateTimeField(db_column="ACTUALIZADO_EN", auto_now=True)

    class Meta:
        db_table = "DGII_ECF_DOCUMENTO"
        ordering = ["-fecha_doc", "-id_doc"]


class FacturacionElectronicaEvento(models.Model):
    id_evento = models.AutoField(db_column="ID_EVENTO", primary_key=True)
    documento = models.ForeignKey(
        FacturacionElectronicaDocumento,
        db_column="ID_DOCUMENTO",
        on_delete=models.CASCADE,
        related_name="eventos",
    )
    tipo_evento = models.CharField(db_column="TIPO_EVENTO", max_length=50)
    detalle = models.TextField(db_column="DETALLE", blank=True, null=True)
    creado_en = models.DateTimeField(db_column="CREADO_EN", auto_now_add=True)

    class Meta:
        db_table = "DGII_ECF_EVENTO"
        ordering = ["-creado_en", "-id_evento"]
