"""grok_stream: full-duplex PCM streaming to the Grok Voice Live bridge (Wyoming-style framing).

Derived from RealtimeClaw's MIT-licensed wyoming_tcp_client component."""
import esphome.codegen as cg
import esphome.config_validation as cv
from esphome import automation
from esphome.automation import maybe_simple_id
from esphome.components import microphone, speaker
from esphome.const import CONF_ID, CONF_MICROPHONE, CONF_PORT, CONF_SPEAKER, CONF_ON_START
from esphome.core import CORE

CODEOWNERS = ["@mtfang"]
DEPENDENCIES = ["microphone", "speaker", "esp32"]
AUTO_LOAD = ["audio", "ring_buffer"]

CONF_HOST = "host"
CONF_TOKEN = "token"
CONF_IDLE_TIMEOUT = "idle_timeout"
CONF_MAX_DURATION = "max_duration"
CONF_ON_STATE = "on_state"
CONF_ON_END = "on_end"

grok_stream_ns = cg.esphome_ns.namespace("grok_stream")
GrokStream = grok_stream_ns.class_("GrokStream", cg.Component)
StartAction = grok_stream_ns.class_("StartAction", automation.Action, cg.Parented.template(GrokStream))
StopAction = grok_stream_ns.class_("StopAction", automation.Action, cg.Parented.template(GrokStream))
IsRunningCondition = grok_stream_ns.class_("IsRunningCondition", automation.Condition, cg.Parented.template(GrokStream))



def _validate_token(value):
    value = cv.string(value)
    if len(value) > 64 or any(not (c.isalnum() or c in "._-") for c in value):
        raise cv.Invalid("token must be <=64 chars of [A-Za-z0-9._-]")
    return value


CONFIG_SCHEMA = cv.Schema(
    {
        cv.GenerateID(): cv.declare_id(GrokStream),
        cv.Required(CONF_HOST): cv.string_strict,
        cv.Optional(CONF_PORT, default=10700): cv.port,
        cv.Optional(CONF_TOKEN, default=""): _validate_token,
        cv.Optional(CONF_MICROPHONE, default={}): microphone.microphone_source_schema(
            min_bits_per_sample=16, max_bits_per_sample=16, min_channels=1, max_channels=1
        ),
        cv.Required(CONF_SPEAKER): cv.use_id(speaker.Speaker),
        cv.Optional(CONF_IDLE_TIMEOUT, default="15s"): cv.positive_time_period_milliseconds,
        cv.Optional(CONF_MAX_DURATION, default="130s"): cv.positive_time_period_milliseconds,
        cv.Optional(CONF_ON_START): automation.validate_automation(single=True),
        cv.Optional(CONF_ON_END): automation.validate_automation(single=True),
        cv.Optional(CONF_ON_STATE): automation.validate_automation(single=True),
    }
).extend(cv.COMPONENT_SCHEMA)

FINAL_VALIDATE_SCHEMA = cv.Schema(
    {cv.Optional(CONF_MICROPHONE): microphone.final_validate_microphone_source_schema("grok_stream", sample_rate=16000)},
    extra=cv.ALLOW_EXTRA,
)


async def to_code(config):
    var = cg.new_Pvariable(config[CONF_ID])
    await cg.register_component(var, config)
    cg.add(var.set_host(config[CONF_HOST]))
    cg.add(var.set_port(config[CONF_PORT]))
    cg.add(var.set_token(config[CONF_TOKEN]))
    cg.add(var.set_device_name(CORE.name))
    cg.add(var.set_idle_timeout(config[CONF_IDLE_TIMEOUT]))
    cg.add(var.set_max_duration(config[CONF_MAX_DURATION]))
    mic_source = await microphone.microphone_source_to_code(config[CONF_MICROPHONE])
    cg.add(var.set_microphone_source(mic_source))
    spk = await cg.get_variable(config[CONF_SPEAKER])
    cg.add(var.set_speaker(spk))
    if CONF_ON_START in config:
        await automation.build_automation(var.get_start_trigger(), [], config[CONF_ON_START])
    if CONF_ON_END in config:
        await automation.build_automation(var.get_end_trigger(), [(cg.std_string, "x")], config[CONF_ON_END])
    if CONF_ON_STATE in config:
        await automation.build_automation(var.get_state_trigger(), [(cg.std_string, "x")], config[CONF_ON_STATE])


GROK_ACTION_SCHEMA = maybe_simple_id({cv.GenerateID(): cv.use_id(GrokStream)})


@automation.register_action("grok_stream.start", StartAction, GROK_ACTION_SCHEMA, synchronous=True)
@automation.register_action("grok_stream.stop", StopAction, GROK_ACTION_SCHEMA, synchronous=True)
async def grok_action_to_code(config, action_id, template_arg, args):
    var = cg.new_Pvariable(action_id, template_arg)
    await cg.register_parented(var, config[CONF_ID])
    return var


@automation.register_condition("grok_stream.is_running", IsRunningCondition, GROK_ACTION_SCHEMA)
async def grok_is_running_to_code(config, condition_id, template_arg, args):
    var = cg.new_Pvariable(condition_id, template_arg)
    await cg.register_parented(var, config[CONF_ID])
    return var
