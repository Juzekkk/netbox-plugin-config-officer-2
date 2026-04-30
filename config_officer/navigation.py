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
                ),
                PluginMenuItem(
                    link="plugins:config_officer:collection_status",
                    link_text="Data Collection Jobs",
                ),
            ),
        ),
        (
            "COMPLIANCE",
            (
                PluginMenuItem(
                    link="plugins:config_officer:template_list",
                    link_text="Templates Configuration",
                ),
                PluginMenuItem(
                    link="plugins:config_officer:service_mapping_list",
                    link_text="Templates Compliance Status",
                ),
            ),
        ),
    ),
)
