"""
Settings Management Module
Handles user settings persistence and access
"""

import copy
import json
import os
import logging


class SettingsManager:
    """Manages user settings persistence"""
    
    DEFAULT_POWER_PROFILES = {
        'mains': {
            'sleep': 'auto',
            'sleep_timeout': 900,
            'network_reading': 'auto',
            'network_sleep': 'auto',
            'reader_prefetch': 10,
        },
        'battery': {
            'sleep': 'auto',
            'sleep_timeout': 300,
            'network_reading': 'auto',
            'network_sleep': 'auto',
            'reader_prefetch': 3,
        },
        'powersave': {
            'sleep': 'auto',
            'sleep_timeout': 120,
            'network_reading': 'auto',
            'network_sleep': 'auto',
            'reader_prefetch': 1,
        },
    }

    DEFAULT_SETTINGS = {
        'zoom': 1.0,
        'full_refresh_interval': 5,
        'show_page_numbers': True,
        'sleep_message': "Shh I'm sleeping",
        'shutdown_message': 'OFF',
        'items_per_page': 4,
        'library_font_size': 20,

        # Power profiles v2.
        'power_mode': 'auto',
        'auto_powersave_enabled': True,
        'auto_powersave_threshold': 30,
        'power_profiles': DEFAULT_POWER_PROFILES,
    }
    
    def _migrate_power_profiles_v2(self, loaded):
        """Migrate legacy energy settings and discard legacy keys."""
        legacy_keys = {
            'dpi',
            'sleep_enabled',
            'sleep_timeout',
            'wifi_while_reading',
            'power_profile',
            'reader_prefetch_mains',
            'reader_prefetch_battery',
            'reader_prefetch_powersave',
        }

        if 'power_profiles' in loaded and 'power_mode' in loaded:
            migrated = copy.deepcopy(loaded)
            for key in legacy_keys:
                migrated.pop(key, None)
            return migrated

        migrated = copy.deepcopy(loaded)

        old_mode = str(
            loaded.get('power_profile', 'battery')
        ).strip().lower()

        migrated['power_mode'] = (
            'auto' if old_mode == 'battery'
            else old_mode if old_mode in {'mains', 'powersave'}
            else 'auto'
        )

        sleep = 'on' if bool(
            loaded.get('sleep_enabled', True)
        ) else 'off'

        network = 'on' if bool(
            loaded.get('wifi_while_reading', False)
        ) else 'off'

        timeout = int(loaded.get('sleep_timeout', 120))

        profiles = copy.deepcopy(self.DEFAULT_POWER_PROFILES)

        for name in ('mains', 'battery', 'powersave'):
            profiles[name]['sleep'] = sleep
            profiles[name]['sleep_timeout'] = timeout
            profiles[name]['network_reading'] = network
            profiles[name]['network_sleep'] = network

        profiles['mains']['reader_prefetch'] = int(
            loaded.get('reader_prefetch_mains', 10)
        )
        profiles['battery']['reader_prefetch'] = int(
            loaded.get('reader_prefetch_battery', 3)
        )
        profiles['powersave']['reader_prefetch'] = int(
            loaded.get('reader_prefetch_powersave', 1)
        )

        migrated['power_profiles'] = profiles

        for key in legacy_keys:
            migrated.pop(key, None)

        return migrated

    def __init__(self, settings_file='settings.json', logger=None):
        """
        Initialize settings manager
        
        Args:
            settings_file: Path to settings JSON file
            logger: Logger instance (optional)
        """
        self.settings_file = settings_file
        self.logger = logger or logging.getLogger(__name__)
        self.settings = self.load()
    
    def load(self):
        """Load settings from file"""
        if os.path.exists(self.settings_file):
            try:
                with open(self.settings_file, 'r') as f:
                    loaded_settings = json.load(f)
                    loaded_settings = self._migrate_power_profiles_v2(
                        loaded_settings
                    )

                    settings = copy.deepcopy(self.DEFAULT_SETTINGS)
                    settings.update(loaded_settings)

                    profiles = copy.deepcopy(
                        self.DEFAULT_POWER_PROFILES
                    )
                    saved_profiles = settings.get(
                        'power_profiles', {}
                    )

                    if isinstance(saved_profiles, dict):
                        for name in profiles:
                            saved = saved_profiles.get(name)
                            if isinstance(saved, dict):
                                profiles[name].update(saved)

                    settings['power_profiles'] = profiles
                    return settings
            except Exception as e:
                self.logger.warning(f"Failed to load settings: {e}. Using defaults.")
                return copy.deepcopy(self.DEFAULT_SETTINGS)
        else:
            # Create default settings file
            self.save(copy.deepcopy(self.DEFAULT_SETTINGS))
            return copy.deepcopy(self.DEFAULT_SETTINGS)
    
    def save(self, settings=None):
        """
        Save settings to file
        
        Args:
            settings: Settings dict to save (uses self.settings if None)
        """
        if settings is not None:
            self.settings = settings
        
        try:
            with open(self.settings_file, 'w') as f:
                json.dump(self.settings, f, indent=2)
            self.logger.info("Settings saved successfully")
        except Exception as e:
            self.logger.error(f"Failed to save settings: {e}")
    
    def get(self, key, default=None):
        """
        Get a setting value
        
        Args:
            key: Setting key
            default: Default value if key doesn't exist
            
        Returns:
            Setting value or default
        """
        return self.settings.get(key, default)
    
    def set(self, key, value):
        """
        Set a setting value
        
        Args:
            key: Setting key
            value: Setting value
        """
        self.settings[key] = value
    
    def update(self, updates):
        """
        Update multiple settings
        
        Args:
            updates: Dict of settings to update
        """
        self.settings.update(updates)
    
    def get_all(self):
        """Get all settings"""
        return self.settings.copy()
