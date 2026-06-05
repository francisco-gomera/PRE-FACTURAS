from django.db import models


class EmpleadoNomina(models.Model):
    id_empleado = models.AutoField(db_column="ID_EMPLEADO", primary_key=True)
    codigo = models.CharField(db_column="CODIGO", max_length=30, unique=True)
    nombres = models.CharField(db_column="NOMBRES", max_length=100)
    apellidos = models.CharField(db_column="APELLIDOS", max_length=100)
    apodo = models.CharField(db_column="APODO", max_length=60, blank=True)
    cedula = models.CharField(db_column="CEDULA", max_length=20, blank=True)
    estado_civil = models.CharField(db_column="ESTADO_CIVIL", max_length=30, blank=True)
    direccion = models.CharField(db_column="DIRECCION", max_length=250, blank=True)
    telefono = models.CharField(db_column="TELEFONO", max_length=30, blank=True)
    celular = models.CharField(db_column="CELULAR", max_length=30, blank=True)
    tipo_sangre = models.CharField(db_column="TIPO_SANGRE", max_length=5, blank=True)
    fecha_nacimiento = models.DateField(db_column="FECHA_NACIMIENTO", blank=True, null=True)
    nacionalidad = models.CharField(db_column="NACIONALIDAD", max_length=80, blank=True)
    genero = models.CharField(db_column="GENERO", max_length=30, blank=True)
    lugar_nacimiento = models.CharField(db_column="LUGAR_NACIMIENTO", max_length=120, blank=True)
    nivel_academico = models.CharField(db_column="NIVEL_ACADEMICO", max_length=80, blank=True)
    email = models.EmailField(db_column="EMAIL", max_length=120, blank=True)
    carnet = models.CharField(db_column="CARNET", max_length=40, blank=True)
    fecha_ingreso = models.DateField(db_column="FECHA_INGRESO", blank=True, null=True)
    salario_base = models.DecimalField(db_column="SALARIO_BASE", max_digits=12, decimal_places=2, blank=True, null=True)
    forma_pago = models.CharField(db_column="FORMA_PAGO", max_length=30, blank=True)
    banco = models.CharField(db_column="BANCO", max_length=100, blank=True)
    cuenta_bancaria = models.CharField(db_column="CUENTA_BANCARIA", max_length=60, blank=True)
    tipo_cuenta = models.CharField(db_column="TIPO_CUENTA", max_length=30, blank=True)
    frecuencia_pago = models.CharField(db_column="FRECUENCIA_PAGO", max_length=30, blank=True)
    clase_empleado = models.CharField(db_column="CLASE_EMPLEADO", max_length=30, blank=True)
    departamento = models.CharField(db_column="DEPARTAMENTO", max_length=100, blank=True)
    cargo = models.CharField(db_column="CARGO", max_length=80, blank=True)
    dias_vacaciones = models.PositiveSmallIntegerField(db_column="DIAS_VACACIONES", default=0)
    supervisor = models.CharField(db_column="SUPERVISOR", max_length=120, blank=True)
    sucursal = models.CharField(db_column="SUCURSAL", max_length=100, blank=True)
    tipo_empleado = models.CharField(db_column="TIPO_EMPLEADO", max_length=30, blank=True)
    poncha = models.BooleanField(db_column="PONCHA", default=False)
    horarios_json = models.TextField(db_column="HORARIOS_JSON", blank=True)
    ars = models.CharField(db_column="ARS", max_length=80, blank=True)
    numero_afiliado = models.CharField(db_column="NUMERO_AFILIADO", max_length=60, blank=True)
    numero_ss = models.CharField(db_column="NUMERO_SS", max_length=60, blank=True)
    pareja_nombre = models.CharField(db_column="PAREJA_NOMBRE", max_length=140, blank=True)
    pareja_telefono = models.CharField(db_column="PAREJA_TELEFONO", max_length=30, blank=True)
    numero_dependientes = models.CharField(db_column="NUMERO_DEPENDIENTES", max_length=10, blank=True)
    contacto_emergencia = models.CharField(db_column="CONTACTO_EMERGENCIA", max_length=140, blank=True)
    celular_emergencia = models.CharField(db_column="CELULAR_EMERGENCIA", max_length=30, blank=True)
    telefono_emergencia = models.CharField(db_column="TELEFONO_EMERGENCIA", max_length=30, blank=True)
    estado = models.CharField(db_column="ESTADO", max_length=30, default="Inactivo")
    observaciones = models.TextField(db_column="OBSERVACIONES", blank=True)
    id_foto = models.IntegerField(db_column="ID_FOTO", blank=True, null=True)
    fecha_creacion = models.DateTimeField(db_column="FECHA_CREACION", auto_now_add=True)
    fecha_modificacion = models.DateTimeField(db_column="FECHA_MODIFICACION", auto_now=True)

    class Meta:
        db_table = "EMPLEADO_NOMINA"
        ordering = ["codigo"]


class EmpleadoFoto(models.Model):
    id_foto = models.AutoField(db_column="ID_FOTO", primary_key=True)
    id_empleado = models.BigIntegerField(db_column="ID_EMPLEADO", unique=True)
    foto = models.BinaryField(db_column="FOTO", blank=True, null=True)
    foto_tipo = models.CharField(db_column="FOTO_TIPO", max_length=80, blank=True)
    creado_en = models.DateTimeField(db_column="CREADO_EN", auto_now_add=True)
    actualizado_en = models.DateTimeField(db_column="ACTUALIZADO_EN", auto_now=True)

    class Meta:
        db_table = "EMPLEADO_FOTO"
        ordering = ["id_empleado"]


class EmpleadoEstudio(models.Model):
    id_estudio = models.AutoField(db_column="ID_ESTUDIO", primary_key=True)
    empleado = models.ForeignKey(
        EmpleadoNomina,
        db_column="ID_EMPLEADO",
        on_delete=models.CASCADE,
        related_name="estudios",
    )
    estudio_realizado = models.CharField(db_column="ESTUDIO_REALIZADO", max_length=160)
    desde = models.DateField(db_column="DESDE", blank=True, null=True)
    hasta = models.DateField(db_column="HASTA", blank=True, null=True)
    lugar_estudio = models.CharField(db_column="LUGAR_ESTUDIO", max_length=160, blank=True)
    telefono = models.CharField(db_column="TELEFONO", max_length=30, blank=True)
    contacto = models.CharField(db_column="CONTACTO", max_length=120, blank=True)

    class Meta:
        db_table = "EMPLEADO_ESTUDIO"
        ordering = ["desde", "id_estudio"]


class EmpleadoExperienciaLaboral(models.Model):
    id_experiencia = models.AutoField(db_column="ID_EXPERIENCIA", primary_key=True)
    empleado = models.ForeignKey(
        EmpleadoNomina,
        db_column="ID_EMPLEADO",
        on_delete=models.CASCADE,
        related_name="experiencias_laborales",
    )
    lugar_trabajo = models.CharField(db_column="LUGAR_TRABAJO", max_length=160)
    desde = models.DateField(db_column="DESDE", blank=True, null=True)
    hasta = models.DateField(db_column="HASTA", blank=True, null=True)
    cargo = models.CharField(db_column="CARGO", max_length=120, blank=True)
    supervisor = models.CharField(db_column="SUPERVISOR", max_length=120, blank=True)
    telefono = models.CharField(db_column="TELEFONO", max_length=30, blank=True)

    class Meta:
        db_table = "EMPLEADO_EXPERIENCIA_LABORAL"
        ordering = ["desde", "id_experiencia"]


class EmpleadoAccionPersonal(models.Model):
    ESTATUS_PENDIENTE = "PENDIENTE"
    ESTATUS_APLICADA = "APLICADA"
    ESTATUS_CANCELADA = "CANCELADA"
    TIPO_ENTRADA = "ENTRADA"
    TIPO_CAMBIO = "CAMBIO"
    TIPO_SALIDA = "SALIDA"

    id_accion = models.AutoField(db_column="ID_ACCION", primary_key=True)
    empleado = models.ForeignKey(
        EmpleadoNomina,
        db_column="ID_EMPLEADO",
        on_delete=models.PROTECT,
        related_name="acciones_personal",
    )
    fecha = models.DateField(db_column="FECHA")
    fecha_efectiva = models.DateField(db_column="FECHA_EFECTIVA")
    estatus = models.CharField(db_column="ESTATUS", max_length=20, default=ESTATUS_PENDIENTE)
    tipo_accion = models.CharField(db_column="TIPO_ACCION", max_length=20)
    afecta_nomina = models.BooleanField(db_column="AFECTA_NOMINA", default=False)
    aplicado = models.BooleanField(db_column="APLICADO", default=False)
    comentario = models.TextField(db_column="COMENTARIO", blank=True)

    entrada_motivo = models.CharField(db_column="ENTRADA_MOTIVO", max_length=80, blank=True)
    entrada_nomina = models.CharField(db_column="ENTRADA_NOMINA", max_length=80, blank=True)
    motivo_nombramiento = models.CharField(db_column="MOTIVO_NOMBRAMIENTO", max_length=80, blank=True)
    contrato_fecha_inicio = models.DateField(db_column="CONTRATO_FECHA_INICIO", blank=True, null=True)
    contrato_fecha_fin = models.DateField(db_column="CONTRATO_FECHA_FIN", blank=True, null=True)
    salario_propuesto = models.DecimalField(db_column="SALARIO_PROPUESTO", max_digits=12, decimal_places=2, blank=True, null=True)

    salida_motivo = models.CharField(db_column="SALIDA_MOTIVO", max_length=80, blank=True)

    cambio_motivo = models.CharField(db_column="CAMBIO_MOTIVO", max_length=80, blank=True)
    cambio_departamento = models.CharField(db_column="CAMBIO_DEPARTAMENTO", max_length=100, blank=True)
    cambio_cargo = models.CharField(db_column="CAMBIO_CARGO", max_length=80, blank=True)
    cambio_nomina = models.CharField(db_column="CAMBIO_NOMINA", max_length=80, blank=True)
    cambio_departamento_anterior = models.CharField(db_column="CAMBIO_DEPARTAMENTO_ANTERIOR", max_length=100, blank=True)
    cambio_cargo_anterior = models.CharField(db_column="CAMBIO_CARGO_ANTERIOR", max_length=80, blank=True)
    cambio_nomina_anterior = models.CharField(db_column="CAMBIO_NOMINA_ANTERIOR", max_length=80, blank=True)
    cambio_salario_actual = models.DecimalField(db_column="CAMBIO_SALARIO_ACTUAL", max_digits=12, decimal_places=2, blank=True, null=True)
    cambio_salario_propuesto = models.DecimalField(db_column="CAMBIO_SALARIO_PROPUESTO", max_digits=12, decimal_places=2, blank=True, null=True)
    cambio_porcentaje = models.DecimalField(db_column="CAMBIO_PORCENTAJE", max_digits=8, decimal_places=2, blank=True, null=True)
    cambio_diferencia = models.DecimalField(db_column="CAMBIO_DIFERENCIA", max_digits=12, decimal_places=2, blank=True, null=True)
    fecha_desde = models.DateField(db_column="FECHA_DESDE", blank=True, null=True)
    fecha_hasta = models.DateField(db_column="FECHA_HASTA", blank=True, null=True)
    cantidad_dias = models.PositiveSmallIntegerField(db_column="CANTIDAD_DIAS", blank=True, null=True)

    creado_en = models.DateTimeField(db_column="CREADO_EN", auto_now_add=True)
    actualizado_en = models.DateTimeField(db_column="ACTUALIZADO_EN", auto_now=True)

    class Meta:
        db_table = "EMPLEADO_ACCION_PERSONAL"
        ordering = ["-fecha", "-id_accion"]


class EmpleadoVacacionBalance(models.Model):
    id_balance = models.AutoField(db_column="ID_BALANCE", primary_key=True)
    empleado = models.ForeignKey(
        EmpleadoNomina,
        db_column="ID_EMPLEADO",
        on_delete=models.CASCADE,
        related_name="balances_vacaciones",
    )
    ano = models.PositiveSmallIntegerField(db_column="ANO")
    dias_disponibles = models.PositiveSmallIntegerField(db_column="DIAS_DISPONIBLES", default=0)
    creado_en = models.DateTimeField(db_column="CREADO_EN", auto_now_add=True)
    actualizado_en = models.DateTimeField(db_column="ACTUALIZADO_EN", auto_now=True)

    class Meta:
        db_table = "EMPLEADO_VACACION_BALANCE"
        constraints = [
            models.UniqueConstraint(fields=["empleado", "ano"], name="uq_empleado_vacacion_balance"),
        ]
        ordering = ["-ano", "empleado_id"]


class EmpleadoVacacionPlanificada(models.Model):
    id_plan = models.AutoField(db_column="ID_PLAN", primary_key=True)
    empleado = models.ForeignKey(
        EmpleadoNomina,
        db_column="ID_EMPLEADO",
        on_delete=models.CASCADE,
        related_name="vacaciones_planificadas",
    )
    fecha_desde = models.DateField(db_column="FECHA_DESDE")
    fecha_hasta = models.DateField(db_column="FECHA_HASTA")
    cantidad_dias = models.PositiveSmallIntegerField(db_column="CANTIDAD_DIAS", default=0)
    nota = models.CharField(db_column="NOTA", max_length=200, blank=True)
    creado_en = models.DateTimeField(db_column="CREADO_EN", auto_now_add=True)
    actualizado_en = models.DateTimeField(db_column="ACTUALIZADO_EN", auto_now=True)

    class Meta:
        db_table = "EMPLEADO_VACACION_PLANIFICADA"
        ordering = ["fecha_desde", "id_plan"]


class EmpleadoVacacionDescuento(models.Model):
    id_descuento = models.AutoField(db_column="ID_DESCUENTO", primary_key=True)
    empleado = models.ForeignKey(
        EmpleadoNomina,
        db_column="ID_EMPLEADO",
        on_delete=models.CASCADE,
        related_name="descuentos_vacaciones",
    )
    dias = models.PositiveSmallIntegerField(db_column="DIAS")
    descripcion = models.CharField(db_column="DESCRIPCION", max_length=200)
    fecha_descuento = models.DateField(db_column="FECHA_DESCUENTO")
    fecha_dias_desde = models.DateField(db_column="FECHA_DIAS_DESDE")
    fecha_dias_hasta = models.DateField(db_column="FECHA_DIAS_HASTA")
    creado_en = models.DateTimeField(db_column="CREADO_EN", auto_now_add=True)

    class Meta:
        db_table = "EMPLEADO_VACACION_DESCUENTO"
        ordering = ["-fecha_descuento", "-id_descuento"]

class NominaPeriodo(models.Model):
    TIPO_SEMANAL = "SEMANAL"
    TIPO_QUINCENAL = "QUINCENAL"
    TIPO_MENSUAL = "MENSUAL"
    ESTATUS_BORRADOR = "BORRADOR"
    ESTATUS_PROCESADA = "PROCESADA"
    ESTATUS_APROBADA = "APROBADA"
    ESTATUS_PAGADA = "PAGADA"
    ESTATUS_ANULADA = "ANULADA"

    id_periodo = models.AutoField(db_column="ID_PERIODO", primary_key=True)
    tipo = models.CharField(db_column="TIPO", max_length=20)
    fecha_desde = models.DateField(db_column="FECHA_DESDE")
    fecha_hasta = models.DateField(db_column="FECHA_HASTA")
    descripcion = models.CharField(db_column="DESCRIPCION", max_length=120, blank=True)
    estatus = models.CharField(db_column="ESTATUS", max_length=20, default=ESTATUS_BORRADOR)

    # Toggles para deducciones legales (deshabilitados por defecto)
    aplicar_afp = models.BooleanField(db_column="APLICAR_AFP", default=False)
    aplicar_sfs = models.BooleanField(db_column="APLICAR_SFS", default=False)
    aplicar_srl = models.BooleanField(db_column="APLICAR_SRL", default=False)
    aplicar_isr = models.BooleanField(db_column="APLICAR_ISR", default=False)

    creado_en = models.DateTimeField(db_column="CREADO_EN", auto_now_add=True)
    actualizado_en = models.DateTimeField(db_column="ACTUALIZADO_EN", auto_now=True)

    class Meta:
        db_table = "NOMINA_PERIODO"
        ordering = ["-fecha_desde", "-id_periodo"]


class NominaEntrada(models.Model):
    id_entrada = models.AutoField(db_column="ID_ENTRADA", primary_key=True)
    periodo = models.ForeignKey(
        NominaPeriodo,
        db_column="ID_PERIODO",
        on_delete=models.CASCADE,
        related_name="entradas",
    )
    empleado = models.ForeignKey(
        EmpleadoNomina,
        db_column="ID_EMPLEADO",
        on_delete=models.PROTECT,
        related_name="nomina_entradas",
    )
    salario_periodo = models.DecimalField(db_column="SALARIO_PERIODO", max_digits=12, decimal_places=2, default=0)
    dias_trabajados = models.DecimalField(db_column="DIAS_TRABAJADOS", max_digits=6, decimal_places=2, default=0)

    # Ingresos adicionales
    horas_extras_35 = models.DecimalField(db_column="HORAS_EXTRAS_35", max_digits=6, decimal_places=2, default=0)
    monto_horas_extras_35 = models.DecimalField(db_column="MONTO_HORAS_EXTRAS_35", max_digits=12, decimal_places=2, default=0)
    horas_extras_100 = models.DecimalField(db_column="HORAS_EXTRAS_100", max_digits=6, decimal_places=2, default=0)
    monto_horas_extras_100 = models.DecimalField(db_column="MONTO_HORAS_EXTRAS_100", max_digits=12, decimal_places=2, default=0)
    bonificacion = models.DecimalField(db_column="BONIFICACION", max_digits=12, decimal_places=2, default=0)
    bonificacion_desc = models.CharField(db_column="BONIFICACION_DESC", max_length=120, blank=True)
    comisiones = models.DecimalField(db_column="COMISIONES", max_digits=12, decimal_places=2, default=0)
    vacaciones_pagadas = models.DecimalField(db_column="VACACIONES_PAGADAS", max_digits=12, decimal_places=2, default=0)
    regalia = models.DecimalField(db_column="REGALIA", max_digits=12, decimal_places=2, default=0)
    otros_ingresos = models.DecimalField(db_column="OTROS_INGRESOS", max_digits=12, decimal_places=2, default=0)
    otros_ingresos_desc = models.CharField(db_column="OTROS_INGRESOS_DESC", max_length=120, blank=True)

    # Deducciones legales
    afp_empleado = models.DecimalField(db_column="AFP_EMPLEADO", max_digits=12, decimal_places=2, default=0)
    afp_empleador = models.DecimalField(db_column="AFP_EMPLEADOR", max_digits=12, decimal_places=2, default=0)
    sfs_empleado = models.DecimalField(db_column="SFS_EMPLEADO", max_digits=12, decimal_places=2, default=0)
    sfs_empleador = models.DecimalField(db_column="SFS_EMPLEADOR", max_digits=12, decimal_places=2, default=0)
    srl_empleador = models.DecimalField(db_column="SRL_EMPLEADOR", max_digits=12, decimal_places=2, default=0)
    isr_retencion = models.DecimalField(db_column="ISR_RETENCION", max_digits=12, decimal_places=2, default=0)

    # Deducciones manuales
    adelanto = models.DecimalField(db_column="ADELANTO", max_digits=12, decimal_places=2, default=0)
    prestamo_descuento = models.DecimalField(db_column="PRESTAMO_DESCUENTO", max_digits=12, decimal_places=2, default=0)
    otras_deducciones = models.DecimalField(db_column="OTRAS_DEDUCCIONES", max_digits=12, decimal_places=2, default=0)
    otras_deducciones_desc = models.CharField(db_column="OTRAS_DEDUCCIONES_DESC", max_length=120, blank=True)

    # Totales
    total_ingresos = models.DecimalField(db_column="TOTAL_INGRESOS", max_digits=12, decimal_places=2, default=0)
    total_deducciones_legales = models.DecimalField(db_column="TOTAL_DEDUCCIONES_LEGALES", max_digits=12, decimal_places=2, default=0)
    total_otras_deducciones = models.DecimalField(db_column="TOTAL_OTRAS_DEDUCCIONES", max_digits=12, decimal_places=2, default=0)
    neto_pagar = models.DecimalField(db_column="NETO_PAGAR", max_digits=12, decimal_places=2, default=0)

    notas = models.TextField(db_column="NOTAS", blank=True)
    creado_en = models.DateTimeField(db_column="CREADO_EN", auto_now_add=True)
    actualizado_en = models.DateTimeField(db_column="ACTUALIZADO_EN", auto_now=True)

    class Meta:
        db_table = "NOMINA_ENTRADA"
        constraints = [
            models.UniqueConstraint(fields=["periodo", "empleado"], name="uq_nomina_entrada_periodo_empleado"),
        ]
        ordering = ["empleado__codigo"]


class NominaAdelanto(models.Model):
    id_adelanto = models.AutoField(db_column="ID_ADELANTO", primary_key=True)
    empleado = models.ForeignKey(
        EmpleadoNomina,
        db_column="ID_EMPLEADO",
        on_delete=models.CASCADE,
        related_name="adelantos",
    )
    monto = models.DecimalField(db_column="MONTO", max_digits=12, decimal_places=2)
    fecha = models.DateField(db_column="FECHA")
    periodo_descuento = models.ForeignKey(
        NominaPeriodo,
        db_column="ID_PERIODO_DESCUENTO",
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="adelantos_descontados",
    )
    descontado = models.BooleanField(db_column="DESCONTADO", default=False)
    nota = models.CharField(db_column="NOTA", max_length=200, blank=True)
    creado_en = models.DateTimeField(db_column="CREADO_EN", auto_now_add=True)

    class Meta:
        db_table = "NOMINA_ADELANTO"
        ordering = ["-fecha", "-id_adelanto"]
