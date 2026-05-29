from netbox.plugins import PluginMenu, PluginMenuItem

menu = PluginMenu(
    label="Config Officer",
    icon_class="mdi mdi-account-tie-hat",
    groups=(
        (
            "COLLECTION",
            (
                PluginMenuItem(
                    link="plugins:config_officer:schedule_list",
                    link_text="Schedule Data Collection",
                    permissions=["dcim.view_device"],
                ),
                PluginMenuItem(
                    link="plugins:config_officer:collection_status",
                    link_text="Data Collection Jobs",
                    permissions=["dcim.view_device"],
                ),
            ),
        ),
        (
            "COMPLIANCE",
            (
                PluginMenuItem(
                    link="plugins:config_officer:template_list",
                    link_text="Templates Configuration",
                    permissions=["dcim.view_device"],
                ),
                PluginMenuItem(
                    link="plugins:config_officer:service_mapping_list",
                    link_text="Templates Compliance Status",
                    permissions=["dcim.view_device"],
                ),
            ),
        ),
    ),
)
