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


class UsuarioCajaPreferencia(models.Model):
    id_preferencia = models.AutoField(db_column="ID_PREFERENCIA", primary_key=True)
    id_usuario = models.BigIntegerField(db_column="ID_USUARIO", unique=True)
    metodo_pago_default = models.CharField(db_column="METODO_PAGO_DEFAULT", max_length=20, blank=True, null=True)
    creado_en = models.DateTimeField(db_column="CREADO_EN", auto_now_add=True)
    actualizado_en = models.DateTimeField(db_column="ACTUALIZADO_EN", auto_now=True)

    class Meta:
        db_table = "CAJA_USUARIO_PREFERENCIA"
        ordering = ["id_usuario"]


class ImpresoraConfig(models.Model):
    TIPO_CUENTAS_COBRAR = "cxc"
    TIPO_FACTURA = "factura"
    TIPO_FINANCIAMIENTO = "financiamiento"
    TIPO_TICKET = "ticket"
    TIPO_CHOICES = [
        (TIPO_CUENTAS_COBRAR, "Cuentas por Cobrar"),
        (TIPO_FACTURA, "Factura"),
        (TIPO_FINANCIAMIENTO, "Financiamiento"),
        (TIPO_TICKET, "Venta POS"),
    ]

    id_config = models.AutoField(db_column="ID_CONFIG", primary_key=True)
    tipo_documento = models.CharField(db_column="TIPO_DOCUMENTO", max_length=40, unique=True, choices=TIPO_CHOICES)
    nombre_impresora = models.CharField(db_column="NOMBRE_IMPRESORA", max_length=255, blank=True, null=True)
    predeterminada = models.BooleanField(db_column="PREDETERMINADA", default=False)
    creado_en = models.DateTimeField(db_column="CREADO_EN", auto_now_add=True)
    actualizado_en = models.DateTimeField(db_column="ACTUALIZADO_EN", auto_now=True)

    class Meta:
        db_table = "AJUSTE_IMPRESORA_CONFIG"
        ordering = ["tipo_documento"]


class FormatoImpresionConfig(models.Model):
    DOCUMENTO_RECIBO_PAGO = "recibo_pago"
    DOCUMENTO_FACTURA = "factura"
    DOCUMENTO_CHOICES = [
        (DOCUMENTO_RECIBO_PAGO, "Recibo de pago"),
        (DOCUMENTO_FACTURA, "Factura"),
    ]
    FORMATO_80MM = "80mm"
    FORMATO_58MM = "58mm"
    FORMATO_A4 = "a4"
    FORMATO_CHOICES = [
        (FORMATO_A4, "A4"),
        (FORMATO_80MM, "80mm"),
        (FORMATO_58MM, "58mm"),
    ]

    id_config = models.AutoField(db_column="ID_CONFIG", primary_key=True)
    documento = models.CharField(db_column="DOCUMENTO", max_length=40, unique=True, choices=DOCUMENTO_CHOICES)
    formato = models.CharField(db_column="FORMATO", max_length=10, choices=FORMATO_CHOICES, default=FORMATO_A4)
    creado_en = models.DateTimeField(db_column="CREADO_EN", auto_now_add=True)
    actualizado_en = models.DateTimeField(db_column="ACTUALIZADO_EN", auto_now=True)

    class Meta:
        db_table = "AJUSTE_FORMATO_IMPRESION"
        ordering = ["documento"]


class FeriadoNacional(models.Model):
    id_feriado = models.AutoField(db_column="ID_FERIADO", primary_key=True)
    fecha = models.DateField(db_column="FECHA", unique=True)
    descripcion = models.CharField(db_column="DESCRIPCION", max_length=160)
    no_laborable = models.BooleanField(db_column="NO_LABORABLE", default=True)
    activo = models.BooleanField(db_column="ACTIVO", default=True)
    creado_en = models.DateTimeField(db_column="CREADO_EN", auto_now_add=True)
    actualizado_en = models.DateTimeField(db_column="ACTUALIZADO_EN", auto_now=True)

    class Meta:
        db_table = "AJUSTE_FERIADO_NACIONAL"
        ordering = ["fecha"]


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


class WhatsAppCloudConfig(models.Model):
    id_config = models.AutoField(db_column="ID_CONFIG", primary_key=True)
    habilitado = models.BooleanField(db_column="HABILITADO", default=False)
    api_version = models.CharField(db_column="API_VERSION", max_length=20, default="v23.0")
    access_token = models.TextField(db_column="ACCESS_TOKEN", blank=True, null=True)
    phone_number_id = models.CharField(db_column="PHONE_NUMBER_ID", max_length=80, blank=True, null=True)
    waba_id = models.CharField(db_column="WABA_ID", max_length=80, blank=True, null=True)
    verify_token = models.CharField(db_column="VERIFY_TOKEN", max_length=255, blank=True, null=True)
    observaciones = models.TextField(db_column="OBSERVACIONES", blank=True, null=True)
    creado_en = models.DateTimeField(db_column="CREADO_EN", auto_now_add=True)
    actualizado_en = models.DateTimeField(db_column="ACTUALIZADO_EN", auto_now=True)

    class Meta:
        db_table = "WHATSAPP_CLOUD_CONFIG"


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
