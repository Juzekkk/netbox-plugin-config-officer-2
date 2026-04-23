from netbox.plugins import PluginMenuItem

menu_items = (
    PluginMenuItem(
        link="plugins:config_officer:collection_status",
        link_text="Device data collection",
    ),
    PluginMenuItem(
        link="plugins:config_officer:template_list",
        link_text="Templates configuration",
    ),
    PluginMenuItem(
        link="plugins:config_officer:service_mapping_list",
        link_text="Templates compliance status",
    ),
    PluginMenuItem(
        link="plugins:config_officer:schedule_list",
        link_text="Schedule data collection",
    ),
)
