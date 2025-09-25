cmake_minimum_required(VERSION 3.10)
project(sonic-host-userd)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

# Include directories (SONiC standard paths)
include_directories(/usr/include/swss)
include_directories(/usr/include)

# Compiler flags
set(CMAKE_CXX_FLAGS "${CMAKE_CXX_FLAGS} -Wall -Wextra -Werror")
set(CMAKE_CXX_FLAGS_DEBUG "${CMAKE_CXX_FLAGS_DEBUG} -g -O0")
set(CMAKE_CXX_FLAGS_RELEASE "${CMAKE_CXX_FLAGS_RELEASE} -O2")

# Add executable
add_executable(userd userd.cpp)

# Link libraries
target_link_libraries(userd
    swsscommon
    pthread
    crypt
)

# Install target
install(TARGETS userd
    RUNTIME DESTINATION /usr/local/bin
)


