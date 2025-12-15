# AppDaemon Automation Scripts

> **Note:** These are personal notes and scripts for recreating my home automation services. They are tailored to my specific hardware setup (Huawei Solar, Shelly devices, Qilowatt service) and may require modification for other environments.

This repository contains three main AppDaemon applications for Home Assistant.

## 1. Load Scheduler (`loads`)

This application manages smart heating and load scheduling based on electricity prices (Nord Pool) and network tariffs.

*   **Inspiration:** This logic draws heavy inspiration from the JavaScript library: [Smart-heating-management-with-Shelly](https://github.com/LeivoSepp/Smart-heating-management-with-Shelly).
*   **Functionality:**
    *   Calculates optimal runtimes for devices (e.g., boilers, heaters) based on electricity prices.
    *   Supports "Period" mode (run N cheapest hours) and "Threshold" mode (run when price is low).
    *   Directly controls Shelly devices via their API for robust scheduling.
*   **Configuration:**
    *   Unlike the other apps, this does **not** use Home Assistant helpers for configuration.
    *   All settings (devices, schedules, network packages) are defined in `apps/loads_config.py`.
    *   Edit `apps/loads_config.py` to add/remove devices or change scheduling parameters.

## 2. Phase Balancer / Inverter Manager (`pbr`)

This application implements the client-side logic for the **Qilowatt.it** inverter management service. It optimizes self-consumption and balances phases by controlling a Huawei Solar inverter and battery system.

*   **Functionality:**
    *   Monitors grid phases and inverter state.
    *   Adjusts battery charging/discharging to balance phases and minimize grid import/export.
    *   Follows control signals from the Qilowatt service (via `sensor.qw_*` entities).
*   **Required Home Assistant Inputs:**
    You must create the following helpers in Home Assistant for this app to function:
    *   `input_boolean.appdaemon_actions`: Master switch to enable/disable the control loop.
    *   `input_boolean.phase_balancer_logging`: Toggle for verbose debug logging.
    *   `input_number.phase_balancer_phase_target`: Target power per phase (in Watts).
    *   `input_number.phase_balancer_range_low`: Lower bound for phase balancing deadband (in Watts).
    *   `input_number.phase_balancer_range_high`: Upper bound for phase balancing deadband (in Watts).

## 3. Europark Parking (`europark_parking`)

This application automates guest parking registration for Europark zones (default: EP90).

*   **Functionality:**
    *   Automatically registers a vehicle for parking when triggered.
    *   Handles authentication and session management with the Europark Partner API.
*   **Required Home Assistant Inputs:**
    *   `input_text.vehicle_registration`: The license plate number to register (e.g., "123ABC").
    *   `input_boolean.europark_api_call_enabled`: Trigger switch. When turned **ON**, the app attempts to register parking for the vehicle defined in the text input.
*   **Secrets:**
    *   Requires `europark_email` and `europark_password` to be defined in `secrets.yaml`.
