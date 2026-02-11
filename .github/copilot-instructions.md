# Copilot Instructions for sonic-host-services

## Project Overview

sonic-host-services provides D-Bus host service modules for SONiC. These services run on the host OS (outside containers) and handle operations that require host-level access — such as configuration reloads, service management, firmware updates, and system operations. Containerized SONiC services communicate with these host modules via D-Bus.

## Architecture

```
sonic-host-services/
├── host_modules/        # D-Bus service modules
│   ├── config_engine.py # Configuration reload/save operations
│   ├── host_service.py  # Base host service class
│   ├── image_service.py # Image management operations
│   └── ...
├── scripts/             # Service scripts and entry points
├── data/                # D-Bus configuration files
├── tests/               # pytest unit tests
├── utils/               # Utility scripts
├── crates/              # Rust components
├── setup.py             # Package setup
├── debian/              # Debian packaging
└── .github/             # GitHub configuration
```

### Key Concepts
- **D-Bus services**: Host modules register as D-Bus services that containers can call
- **Host-container boundary**: These services bridge the gap between containers and host OS
- **Privileged operations**: Operations that need root/host access (config save, reboot, etc.)
- **Security boundary**: D-Bus provides access control for cross-container communication

## Language & Style

- **Primary language**: Python 3
- **Indentation**: 4 spaces
- **Naming conventions**:
  - Modules: `snake_case.py`
  - Classes: `PascalCase`
  - Functions: `snake_case`
  - D-Bus service names: `org.SONiC.*` format
- **Docstrings**: Required for service methods

## Build Instructions

```bash
# Install for development
pip3 install -e .

# Build Debian package
dpkg-buildpackage -us -uc -b

# Build wheel
python3 setup.py bdist_wheel
```

## Testing

```bash
# Run all tests
pytest tests/ -v

# Run with coverage
pytest tests/ --cov=host_modules --cov-report=term-missing
```

- Tests use **pytest** with mock D-Bus interfaces
- Tests verify service registration, method handling, and error paths
- No real D-Bus or hardware access needed for unit tests

## PR Guidelines

- **Commit format**: `[module]: Description`
- **Signed-off-by**: REQUIRED (`git commit -s`)
- **CLA**: Sign Linux Foundation EasyCLA
- **Security**: Extra caution — these services run with elevated privileges
- **D-Bus interface**: Document any new D-Bus methods and their parameters
- **Testing**: All new host modules must have unit tests

## Common Patterns

### D-Bus Service Module
```python
import dbus
import dbus.service

class MyHostService(dbus.service.Object):
    """Host service for my feature"""
    
    @dbus.service.method('org.SONiC.MyService',
                         in_signature='s', out_signature='i')
    def do_operation(self, param):
        """Perform privileged operation on host"""
        try:
            # Execute host-level operation
            return 0  # Success
        except Exception as e:
            return -1  # Failure
```

## Dependencies

- **dbus-python**: Python D-Bus bindings
- **sonic-py-common**: Common SONiC utilities
- **python-swsscommon**: Redis database bindings
- **systemd**: Service management

## Gotchas

- **Security implications**: These services run with root privileges — validate all inputs
- **D-Bus access control**: Ensure proper D-Bus policy configuration in `data/`
- **Container isolation**: Understand which containers can access which D-Bus methods
- **Error handling**: Never let exceptions propagate to D-Bus callers unhandled
- **Blocking operations**: D-Bus calls should not block for extended periods
- **Backwards compatibility**: Don't change D-Bus method signatures — it breaks callers
- **Testing without D-Bus**: Mock the D-Bus layer completely in unit tests
