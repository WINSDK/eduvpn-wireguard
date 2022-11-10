from eduvpn.app import Application
from eduvpn.utils import cmd_transition, init_logger, run_in_background_thread
from eduvpn.ui.search import ServerGroup, group_servers
from eduvpn.ui.utils import get_validity_text
import eduvpn.nm as nm
from eduvpn.i18n import country, retrieve_country_name
from eduvpn.server import ServerDatabase
from eduvpn.settings import (
    CLIENT_ID,
    CONFIG_PREFIX,
    CONFIG_DIR_MODE,
    LETSCONNECT_CLIENT_ID,
    LETSCONNECT_CONFIG_PREFIX,
)
from eduvpn.variants import ApplicationVariant, EDUVPN, LETS_CONNECT
import eduvpn_common.main as common
from eduvpn_common.server import Server, InstituteServer, SecureInternetServer
from eduvpn_common.state import State, StateType

import argparse
import signal
import sys


def get_grouped_index(servers, index):
    if index < 0 or index >= len(servers):
        return None

    grouped_servers = group_servers(servers)
    servers_added = grouped_servers[ServerGroup.INSTITUTE_ACCESS]
    servers_added += grouped_servers[ServerGroup.SECURE_INTERNET]
    servers_added += grouped_servers[ServerGroup.OTHER]
    return servers_added[index]


def ask_profiles(app, profiles):
    # There is only a single profile
    if len(profiles.profiles) == 1:
        print("There is only a single profile for this server:", profiles.profiles[0])
        app.model.set_profile(profiles.profiles[0])
        return

    # Multiple profiles, print the index
    for index, profile in enumerate(profiles.profiles):
        print(f"[{index+1}]: {str(profile)}")

    # And ask for the 1 based index
    while True:
        profile_nr = input("Please select a profile number to continue connecting: ")
        try:
            profile_index = int(profile_nr)

            if profile_index < 1 or profile_index > len(profiles.profiles):
                print(f"Invalid profile choice: {profile_index}")
                continue
            app.model.set_profile(profiles.profiles[profile_index - 1])
            return
        except ValueError:
            print(f"Input is not a number: {profile_nr}")


def ask_locations(app, locations):
    for index, location in enumerate(locations):
        print(f"[{index+1}]: {retrieve_country_name(location)}")

    while True:
        location_nr = input("Please select a location to continue: ")
        try:
            location_index = int(location_nr)

            if location_index < 1 or location_index > len(locations):
                print(f"Invalid location choice: {location_index}")
                continue
            app.model.set_secure_location(locations[location_index - 1])
            return
        except ValueError:
            print(f"Input is not a number: {location_nr}")


class CommandLine:
    def __init__(self, name: str, variant: ApplicationVariant, common):
        self.name = name
        self.variant = variant
        self.common = common
        self.app = Application(variant, common)
        self.nm_manager = self.app.nm_manager
        self.server_db = ServerDatabase(common, variant.use_predefined_servers)
        self.transitions = CommandLineTransitions(self.app)
        self.common.register_class_callbacks(self.transitions)

    def ask_yes(self, label) -> bool:
        while True:
            yesno = input(label)

            if yesno in ["y", "yes"]:
                return True
            if yesno in ["n", "no"]:
                return False
            print(f'Input "{yesno}" is not valid')

    def ask_server_input(self, servers, fallback_search=False):
        print("Multiple servers found:")
        self.list_groups(group_servers(servers))

        while True:
            if fallback_search:
                server_nr = input(
                    "\nPlease select a server number or enter a search query: "
                )
            else:
                server_nr = input("\nPlease select a server number: ")
            try:
                server_index = int(server_nr)
                server = get_grouped_index(servers, server_index - 1)
                if not server:
                    print(f"Invalid server number: {server_index}")
                else:
                    return server
            except ValueError:
                print(f"Input is not a number: {server_nr}")

                if fallback_search:
                    print("Using the term as a search query instead...")
                    server = self.get_server_search(server_nr)
                    if server:
                        return server

    def get_server_search(self, search_query):
        servers = self.server_db.disco
        servers = list(self.server_db.search_predefined(search_query))
        if search_query.count(".") >= 2:
            servers.append(Server(search_query, search_query))

        if len(servers) == 0:
            print(f"No servers found with query: {search_query}")
            return None

        if len(servers) == 1:
            server = servers[0]
            print(f'One server found: "{str(server)}" ({server.category})')
            is_yes = self.ask_yes("Do you want to connect to it (y/n): ")

            if is_yes:
                return servers[0]
            else:
                return None

        return self.ask_server_input(servers)

    def connect_server(self, server):
        def connect(callback=None):
            try:

                @run_in_background_thread("connect")
                def connect_background():
                    # Connect to the server and ensure it exists
                    self.app.model.connect(server, callback, ensure_exists=True)

                connect_background()
            except Exception as e:
                print("Error connecting:", e, file=sys.stderr)
                if callback:
                    callback()

        if not server:
            print("No server found to connect to, exiting...")
            return

        nm.action_with_mainloop(connect)

    def ask_server_custom(self):
        custom = input("Enter a URL to connect to: ")
        return Server(custom, custom)

    def ask_server(self):
        if not self.variant.use_predefined_servers:
            return self.ask_server_custom()

        if self.server_db.configured:
            is_yes = self.ask_yes(
                "Do you want to connect to an existing server? (y/n): "
            )

            if is_yes:
                return self.ask_server_input(self.server_db.configured)

        servers = self.server_db.disco
        return self.ask_server_input(servers, fallback_search=True)

    def parse_server(self, variables):
        search_query = variables.get("search", None)
        url = variables.get("url", None)
        custom_url = variables.get("custom_url", None)
        org_id = variables.get("orgid", None)
        number = variables.get("number", None)
        number_all = variables.get("number_all", None)

        if search_query:
            server = self.get_server_search(search_query)
        elif url:
            server = InstituteServer(url, "Institute Server", [], None, 0)
        elif org_id:
            server = SecureInternetServer(
                org_id, "Organisation Server", [], None, 0, "nl"
            )
        elif custom_url:
            server = Server(custom_url, "Custom Server")
        elif number is not None:
            server = get_grouped_index(self.server_db.configured, number - 1)
            if not server:
                print(f"Configured server with number: {number} does not exist")
        elif number_all is not None:
            servers = self.server_db.disco
            server = get_grouped_index(servers, number_all - 1)
            if not server:
                print(
                    f"Server with number: {number_all} does not exist. Maybe the server list had an update? Make sure to re-run list --all"
                )
        return server

    def status(self, _args={}):
        if not self.app.model.is_connected():
            print("You are currently not connected to a server", file=sys.stderr)
            return False

        current = self.app.model.current_server
        print(f'Connected to: "{str(current)}" ({current.category})')
        expiry = self.app.model.current_server.expire_time
        valid_for = (
            get_validity_text(self.app.model.get_expiry(expiry))[1]
            .replace("<b>", "")
            .replace("</b>", "")
        )
        print(valid_for)
        print(f"Current profile: {str(self.app.model.current_server.profiles.current)}")

    def connect(self, variables={}):
        if self.app.model.is_connected():
            print(
                "You are already connected to a server, please disconnect first",
                file=sys.stderr,
            )
            return False

        if not variables:
            server = self.ask_server()
        else:
            server = self.parse_server(variables)

        return self.connect_server(server)

    def disconnect(self, _arg={}):
        if not self.app.model.is_connected():
            print("You are not connected to a server", file=sys.stderr)
            return False

        def disconnect(callback=None):
            try:
                self.app.model.deactivate_connection(callback)
            except Exception as e:
                print("An error occurred while trying to disconnect")
                print("Error disconnecting:", e, file=sys.stderr)
                if callback:
                    callback()

        nm.action_with_mainloop(disconnect)

    def list_groups(self, grouped_servers):
        total_servers = 1
        if len(grouped_servers[ServerGroup.INSTITUTE_ACCESS]) > 0:
            print("============================")
            print("Institute Access Servers")
            print("============================")
        for institute in grouped_servers[ServerGroup.INSTITUTE_ACCESS]:
            print(f"[{total_servers}]: {str(institute)}")
            total_servers += 1

        if len(grouped_servers[ServerGroup.SECURE_INTERNET]) > 0:
            print("============================")
            print("Secure Internet Server")
            print("============================")
        for secure in grouped_servers[ServerGroup.SECURE_INTERNET]:
            print(f"[{total_servers}]: {str(secure)}")
            total_servers += 1

        if len(grouped_servers[ServerGroup.OTHER]) > 0:
            print("============================")
            print("Custom Servers")
            print("============================")
        for custom in grouped_servers[ServerGroup.OTHER]:
            print(f"[{total_servers}]: {str(custom)}")
            total_servers += 1
        if total_servers > 1:
            print("The number for the server is in [brackets]")

    def list(self, args={}):
        servers = self.server_db.configured
        if args.get("all"):
            servers = self.server_db.disco
        self.list_groups(group_servers(servers))

    def remove_server(self, server):
        if not server:
            print("No server chosen to remove", file=sys.stderr)
            return False

        self.app.model.remove(server)

    def change_profile(self, args={}):
        if not self.app.model.is_connected():
            print(
                "Please connect to a server first before changing profiles",
                file=sys.stderr,
            )
            return False

        server = self.app.model.current_server

        print("Current profile:", server.profiles.current)

        ask_profiles(self.app, server.profiles)

    def change_location(self, args={}):
        if not self.app.model.is_connected():
            print(
                "Please connect to a secure internet server first before changing locations",
                file=sys.stderr,
            )
            return False

        server = self.app.model.current_server
        if not isinstance(server, SecureInternetServer):
            print(
                "The currently connected server is not a secure internet server",
                file=sys.stderr,
            )
            return False

        print("Current location:", retrieve_country_name(server.country_code))

        ask_locations(self.app, server.locations)

        @run_in_background_thread("change-location-reconnect")
        def reconnect(callback=None):
            def on_connect():
                if callback:
                    callback()

            def on_disconnect():
                self.app.model.activate_connection(on_connect)

            # Reconnect
            self.app.model.deactivate_connection(on_disconnect)

        print("Reconnecting with the configured location...")
        nm.action_with_mainloop(reconnect)

    def renew(self, args={}):
        if not self.app.model.is_connected():
            print("Please connect to a server first before renewing", file=sys.stderr)
            return False

        def renew(callback):
            try:

                @run_in_background_thread("renew")
                def renew_background():
                    self.app.model.renew_session()
                    if callback:
                        callback()

                renew_background()
            except Exception as e:
                print("An error occurred while trying to renew")
                print("Error renewing:", e, file=sys.stderr)
                if callback:
                    callback()

        nm.action_with_mainloop(renew)

    def remove(self, args={}):
        if self.app.model.is_connected():
            print(
                "Please disconnect from your server before doing any changes",
                file=sys.stderr,
            )
            return False

        if not self.server_db.configured:
            print("There are no servers configured to remove", file=sys.stderr)
            return False

        if not args:
            server = self.ask_server_input(self.server_db.configured)
        else:
            number = args.get("number", None)
            if number is None:
                print("Please enter a number")
                return
            server = get_grouped_index(self.server_db.configured, number - 1)
            if not server:
                print(f"Configured server with number: {number} does not exist")
        return self.remove_server(server)

    def help_interactive(self):
        print(
            "Available commands: change-location, change-profile, connect, disconnect, renew, remove, status, list, help, quit"
        )

    def update_state(self, initial: bool = False):
        def update_state_callback(callback):
            state = self.nm_manager.connection_state
            self.app.on_network_update_callback(state, initial)

            # This exits the main loop and gives back control to the CLI
            callback()

        nm.action_with_mainloop(update_state_callback)

    def handle_exit(self):
        def signal_handler(_signal, _frame):
            if self.app.model.is_oauth_started():
                self.common.cancel_oauth()
            self.common.go_back()
            self.common.deregister()
            sys.exit(1)

        signal.signal(signal.SIGINT, signal_handler)

    def interactive(self, _):
        # Show a title and the help
        print(f"Welcome to the {self.name} interactive commandline")
        self.help_interactive()
        command = ""
        while command != "quit":
            # Ask for the command to execute
            command = input(f"[{self.name}]: ")

            # Update the state right before we execute
            self.update_state()

            # Execute the right command
            commands = {
                "change-location": self.change_location,
                "change-profile": self.change_profile,
                "connect": self.connect,
                "disconnect": self.disconnect,
                "renew": self.renew,
                "remove": self.remove,
                "status": self.status,
                "list": self.list,
                "help": self.help_interactive,
                "quit": lambda: print("Exiting..."),
            }
            func = commands.get(command, self.help_interactive)
            func()

    def start(self):
        parser = argparse.ArgumentParser(
            description=f"The {self.name} command line client"
        )
        parser.set_defaults(func=lambda _: parser.print_usage())
        parser.add_argument(
            "-d", "--debug", action="store_true", help="enable debugging"
        )
        subparsers = parser.add_subparsers(title="subcommands")

        interactive_parser = subparsers.add_parser(
            "interactive", help="an interactive version of the command line"
        )
        interactive_parser.set_defaults(func=self.interactive)

        renew_parser = subparsers.add_parser(
            "renew", help="renew the validity for the currently connected server"
        )
        renew_parser.set_defaults(func=self.renew)

        change_profile_parser = subparsers.add_parser(
            "change-location",
            help="change the location for the currently connected secure internet server",
        )
        change_profile_parser.set_defaults(func=self.change_location)

        change_profile_parser = subparsers.add_parser(
            "change-profile",
            help="change the profile for the currently connected server",
        )
        change_profile_parser.set_defaults(func=self.change_profile)

        connect_parser = subparsers.add_parser("connect", help="connect to a server")
        connect_group = connect_parser.add_mutually_exclusive_group(required=True)
        if self.variant.use_predefined_servers:
            connect_group.add_argument(
                "-s",
                "--search",
                type=str,
                help="connect to a server by searching for one",
            )
            connect_group.add_argument(
                "-o",
                "--orgid",
                type=str,
                help="connect to a secure internet server using the organisation ID",
            )
            connect_group.add_argument(
                "-u",
                "--url",
                type=str,
                help="connect to an institute access server using the URL",
            )
        connect_group.add_argument(
            "-c",
            "--custom-url",
            type=str,
            help="connect to a custom server using the URL",
        )
        connect_group.add_argument(
            "-n",
            "--number",
            type=int,
            help="connect to an already configured server using the number. Run the 'list' subcommand to see the currently configured servers with their number",
        )
        if self.variant.use_predefined_servers:
            connect_group.add_argument(
                "-a",
                "--number-all",
                type=int,
                help="connect to a server using the number for all servers. Run the 'list --all' command to see all the available servers with their number",
            )
        connect_group.set_defaults(func=lambda args: self.connect(vars(args)))

        disconnect_parser = subparsers.add_parser(
            "disconnect", help="disconnect the currently active server"
        )
        disconnect_parser.set_defaults(func=self.disconnect)

        list_parser = subparsers.add_parser("list", help="list all configured servers")
        if self.variant.use_predefined_servers:
            list_parser.add_argument(
                "--all", action="store_true", help="list all available servers"
            )
        list_parser.set_defaults(func=lambda args: self.list(vars(args)))

        remove_parser = subparsers.add_parser(
            "remove", help="remove a configured server"
        )
        remove_parser.add_argument(
            "--number",
            type=int,
            required=True,
            help="remove a configured server by number",
        )
        remove_parser.set_defaults(func=lambda args: self.remove(vars(args)))

        status_parser = subparsers.add_parser(
            "status", help="see the current status of eduVPN"
        )
        status_parser.set_defaults(func=self.status)

        parsed = parser.parse_args()

        # Handle ctrl+c
        self.handle_exit()

        init_logger(parsed.debug, self.variant.logfile, CONFIG_DIR_MODE)

        # Register the common library
        self.common.register(parsed.debug)

        # Update the state by asking NetworkManager
        self.update_state(True)

        # Run the command
        parsed.func(parsed)

        # Deregister the library
        self.common.deregister()


class CommandLineTransitions:
    def __init__(self, app):
        self.app = app

    @cmd_transition(State.ASK_LOCATION, StateType.ENTER)
    def on_ask_location(self, old_state: State, locations):
        print(
            "This Secure Internet Server has the multiple available locations. Please choose one to continue..."
        )
        ask_locations(self.app, locations)

    @cmd_transition(State.ASK_PROFILE, StateType.ENTER)
    def on_ask_profile(self, old_state: State, profiles):
        print("This server has multiple profiles. Please choose one to continue...")
        ask_profiles(self.app, profiles)

    @cmd_transition(State.OAUTH_STARTED, StateType.ENTER)
    def on_oauth_started(self, old_state: State, url: str):
        print(f"Authorization needed. Your browser has been opened with url: {url}")


def eduvpn():
    _common = common.EduVPN(CLIENT_ID, str(CONFIG_PREFIX), country())
    cmd = CommandLine("eduVPN", EDUVPN, _common)
    cmd.start()


def letsconnect():
    _common = common.EduVPN(
        LETSCONNECT_CLIENT_ID, str(LETSCONNECT_CONFIG_PREFIX), country()
    )
    cmd = CommandLine("Let's Connect!", LETS_CONNECT, _common)
    cmd.start()
