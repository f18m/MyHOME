""" MyHOME integration. """
import aiofiles
import yaml

from OWNd.message import OWNCommand, OWNGatewayCommand

from homeassistant.config_entries import SOURCE_REAUTH, ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.const import CONF_MAC

from .const import (
    ATTR_GATEWAY,
    ATTR_MESSAGE,
    CONF_PLATFORMS,
    CONF_ENTITY,
    CONF_ENTITIES,
    CONF_GATEWAY,
    CONF_WORKER_COUNT,
    CONF_FILE_PATH,
    CONF_GENERATE_EVENTS,
    DOMAIN,
    LOGGER,
)
from .validate import config_schema, format_mac
from .gateway import MyHOMEGatewayHandler

PLATFORMS = ["light", "switch", "cover", "climate", "binary_sensor", "sensor"]


async def async_setup(hass, config):
    """Set up the MyHOME component."""
    hass.data[DOMAIN] = {}

    if DOMAIN not in config:
        return True

    LOGGER.error("configuration.yaml not supported for this component!")

    return False


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry):

    mac = entry.data[CONF_MAC]
    if mac not in hass.data[DOMAIN]:
        hass.data[DOMAIN][mac] = {}

    _config_file_path = (
        str(entry.options[CONF_FILE_PATH])
        if CONF_FILE_PATH in entry.options
        else "/config/myhome.yaml"
    )
    _generate_events = (
        entry.options[CONF_GENERATE_EVENTS]
        if CONF_GENERATE_EVENTS in entry.options
        else False
    )

    try:
        async with aiofiles.open(_config_file_path, mode="r") as yaml_file:
            _validated_config = config_schema(yaml.safe_load(await yaml_file.read()))
    except FileNotFoundError as e:
        LOGGER.error(f"Configuration file '{_config_file_path}' is not present: %s", e)
        return False

    if mac in _validated_config:
        hass.data[DOMAIN][mac] = _validated_config[mac]
    else:
        LOGGER.error("Configuration file '%s' does not contain any configuration for the gateway with MAC address '%s'. Failing the configuration.", 
                     _config_file_path, mac)
        return False

    # Migrating the config entry's unique_id if it was not formatted to the recommended hass standard
    if entry.unique_id != dr.format_mac(entry.unique_id):
        hass.config_entries.async_update_entry(
            entry, unique_id=dr.format_mac(entry.unique_id)
        )
        LOGGER.warning("Migrating config entry unique_id to %s", entry.unique_id)

    hass.data[DOMAIN][mac][CONF_ENTITY] = MyHOMEGatewayHandler(
        hass=hass, config_entry=entry, generate_events=_generate_events
    )

    try:
        tests_results = await hass.data[DOMAIN][mac][CONF_ENTITY].test()
    except OSError as ose:
        _gateway_handler = hass.data[DOMAIN].pop(CONF_GATEWAY)
        _host = _gateway_handler.gateway.host
        raise ConfigEntryNotReady(
            f"Gateway cannot be reached at {_host}, make sure its address is correct."
        ) from ose

    if not tests_results["Success"]:
        if (
            tests_results["Message"] == "password_error"
            or tests_results["Message"] == "password_required"
        ):
            hass.async_create_task(
                hass.config_entries.flow.async_init(
                    DOMAIN,
                    context={"source": SOURCE_REAUTH},
                    data=entry.data,
                )
            )
        del hass.data[DOMAIN][mac][CONF_ENTITY]
        LOGGER.error("Failed configuration of gateway with MAC address '%s': %s", mac, tests_results["Message"])
        return False

    _command_worker_count = (
        int(entry.options[CONF_WORKER_COUNT])
        if CONF_WORKER_COUNT in entry.options
        else 1
    )

    entity_registry = er.async_get(hass)
    device_registry = dr.async_get(hass)

    LOGGER.info(
        "Registering gateway with name '%s' and MAC address '%s'",
        hass.data[DOMAIN][mac][CONF_ENTITY].name, mac
    )
    gateway_entry = device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        connections={(dr.CONNECTION_NETWORK_MAC, mac)},
        identifiers={
            (DOMAIN, hass.data[DOMAIN][mac][CONF_ENTITY].unique_id)
        },
        manufacturer=hass.data[DOMAIN][mac][CONF_ENTITY].manufacturer,
        name=hass.data[DOMAIN][mac][CONF_ENTITY].name,
        model=hass.data[DOMAIN][mac][CONF_ENTITY].model,
        sw_version=hass.data[DOMAIN][mac][CONF_ENTITY].firmware,
    )

    await hass.config_entries.async_forward_entry_setups(entry, hass.data[DOMAIN][mac][CONF_PLATFORMS].keys())

    hass.data[DOMAIN][mac][CONF_ENTITY].listening_worker = hass.loop.create_task(
        hass.data[DOMAIN][mac][CONF_ENTITY].listening_loop()
    )
    for i in range(_command_worker_count):
        hass.data[DOMAIN][mac][CONF_ENTITY].sending_workers.append(
            hass.loop.create_task(
                hass.data[DOMAIN][mac][CONF_ENTITY].sending_loop(i)
            )
        )

    # Pruning lost entities and devices from the registry
    entity_entries = er.async_entries_for_config_entry(entity_registry, entry.entry_id)

    entities_to_be_removed = []
    devices_to_be_removed = [
        device_entry.id
        for device_entry in device_registry.devices.values()
        if entry.entry_id in device_entry.config_entries
    ]

    if gateway_entry.id in devices_to_be_removed:
        devices_to_be_removed.remove(gateway_entry.id)

    configured_entities = []

    for _platform in hass.data[DOMAIN][mac][CONF_PLATFORMS].keys():
        for _device in hass.data[DOMAIN][mac][CONF_PLATFORMS][_platform].keys():
            for _entity_name in hass.data[DOMAIN][mac][CONF_PLATFORMS][_platform][_device][CONF_ENTITIES]:
                LOGGER.info("Registering entity %s", _entity_name)

                if _entity_name != _platform:
                    configured_entities.append(
                        f"{mac}-{_device}-{_entity_name}"
                    )  # extrapolating _attr_unique_id out of the entity's place in the config data structure
                else:
                    configured_entities.append(
                        f"{mac}-{_device}"
                    )  # extrapolating _attr_unique_id out of the entity's place in the config data structure

    for entity_entry in entity_entries:
        if entity_entry.unique_id in configured_entities:
            if entity_entry.device_id in devices_to_be_removed:
                devices_to_be_removed.remove(entity_entry.device_id)
            continue
        entities_to_be_removed.append(entity_entry.entity_id)

    for enity_id in entities_to_be_removed:
        entity_registry.async_remove(enity_id)

    for device_id in devices_to_be_removed:
        if (
            len(
                er.async_entries_for_device(
                    entity_registry, device_id, include_disabled_entities=True
                )
            )
            == 0
        ):
            device_registry.async_remove_device(device_id)

    # Defining the services
    async def handle_sync_time(call):
        gateway = call.data.get(ATTR_GATEWAY, None)
        if gateway is None:
            gateway = list(hass.data[DOMAIN].keys())[0]
        else:
            mac = format_mac(gateway)
            if mac is None:
                LOGGER.error(
                    "Invalid gateway mac `%s`, could not send time synchronisation message.",
                    gateway,
                )
                return False
            else:
                gateway = mac
        timezone = hass.config.as_dict()["time_zone"]
        if gateway in hass.data[DOMAIN]:
            await hass.data[DOMAIN][gateway][CONF_ENTITY].send(
                OWNGatewayCommand.set_datetime_to_now(timezone)
            )
        else:
            LOGGER.error(
                "Gateway `%s` not found, could not send time synchronisation message.",
                gateway,
            )
            return False

    hass.services.async_register(DOMAIN, "sync_time", handle_sync_time)

    async def handle_send_message(call):
        gateway = call.data.get(ATTR_GATEWAY, None)
        message = call.data.get(ATTR_MESSAGE, None)
        if gateway is None:
            gateway = list(hass.data[DOMAIN].keys())[0]
        else:
            mac = format_mac(gateway)
            if mac is None:
                LOGGER.error(
                    "Invalid gateway mac `%s`, could not send message `%s`.",
                    gateway,
                    message,
                )
                return False
            else:
                gateway = mac
        LOGGER.debug("Handling message `%s` to be sent to `%s`", message, gateway)
        if gateway in hass.data[DOMAIN]:
            if message is not None:
                own_message = OWNCommand.parse(message)
                if own_message is not None:
                    if own_message.is_valid:
                        LOGGER.debug(
                            "%s Sending valid OpenWebNet Message: `%s`",
                            hass.data[DOMAIN][gateway][CONF_ENTITY].log_id,
                            own_message,
                        )
                        await hass.data[DOMAIN][gateway][CONF_ENTITY].send(own_message)
                else:
                    LOGGER.error(
                        "Could not parse message `%s`, not sending it.", message
                    )
                    return False
        else:
            LOGGER.error(
                "Gateway `%s` not found, could not send message `%s`.", gateway, message
            )
            return False

    hass.services.async_register(DOMAIN, "send_message", handle_send_message)

    return True


async def async_unload_entry(hass, entry):
    """Unload a config entry."""

    LOGGER.info("Unloading MyHome entry.")

    for platform in hass.data[DOMAIN][entry.data[CONF_MAC]][CONF_PLATFORMS].keys():
        await hass.config_entries.async_forward_entry_unload(entry, platform)

    hass.services.async_remove(DOMAIN, "sync_time")
    hass.services.async_remove(DOMAIN, "send_message")

    gateway_handler = hass.data[DOMAIN][entry.data[CONF_MAC]].pop(CONF_ENTITY)
    del hass.data[DOMAIN][entry.data[CONF_MAC]]

    return await gateway_handler.close_listener()
