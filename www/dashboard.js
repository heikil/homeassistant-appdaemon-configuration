
import { createApp, ref, computed, onMounted, nextTick, watch } from 'vue';

const app = createApp({
    setup() {
        // State
        const activeTab = ref('schedule');
        const isConnected = ref(false);
        const lastUpdate = ref(new Date());
        
        const pbr = ref({
            mode: 'NORMAL',
            l1: 0, l2: 0, l3: 0,
            soc: 0,
            grid_power: 0,
            load_power: 0,
            battery_power: 0,
            heating_active: false
        });
        
        const loads = ref({
            devices: [],
            prices: [],
            weather: null,
            package: '--'
        });
        
        const historyData = ref({
            snapshots: [],
            events: []
        });

        const recentLogs = ref([]);

        // Config
        const LOAD_API = '/api/appdaemon/load_scheduler_data';
        const PBR_API = '/api/appdaemon/pbr';
        let pollTimer = null;
        let scheduleChart = null;
        let historyChart = null;

        // Computed
        const connectionStatus = computed(() => isConnected.value ? 'Live' : 'Offline');
        const connectionStatusColor = computed(() => isConnected.value ? 'bg-green-500' : 'bg-red-500');
        const connectionStatusTextClass = computed(() => isConnected.value ? 'text-green-400' : 'text-red-400');
        
        const currentTime = ref('');
        setInterval(() => {
            currentTime.value = new Date().toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit', second: '2-digit' });
        }, 1000);

        const modeColorClass = computed(() => {
            switch(pbr.value.mode) {
                case 'frrdown': return 'text-purple-400';
                case 'buying': return 'text-green-400';
                case 'selling': return 'text-red-400';
                case 'peakshaving': return 'text-orange-400';
                default: return 'text-blue-400';
            }
        });

        const loadsStats = computed(() => {
            if (!loads.value.prices.length) return { avgPrice: '--', minPrice: '--', totalHours: 0 };
            
            const prices = loads.value.prices.map(p => p.price);
            const avg = prices.reduce((a, b) => a + b, 0) / prices.length;
            const min = Math.min(...prices);
            
            const totalHours = loads.value.devices.reduce((acc, d) => acc + (d.total_hours || 0), 0);

            return {
                avgPrice: avg.toFixed(2),
                minPrice: min.toFixed(2),
                totalHours: totalHours.toFixed(1),
                package: loads.value.package,
                weather: loads.value.weather
            };
        });

        const totalDebt = computed(() => {
            return loads.value.devices.reduce((acc, d) => acc + (d.energy_debt || 0), 0);
        });

        // Methods
        const formatPower = (w) => {
            if (Math.abs(w) >= 1000) return (w / 1000).toFixed(2) + ' kW';
            return Math.round(w) + ' W';
        };

        const fetchAll = async () => {
            try {
                const [pbrRes, loadsRes] = await Promise.all([
                    fetch(PBR_API),
                    fetch(LOAD_API)
                ]);

                if (pbrRes.ok) {
                    const data = await pbrRes.json();
                    pbr.value = data.status;
                    historyData.value = data.history;
                    updateHistory(data.history);
                }
                
                if (loadsRes.ok) {
                    const data = await loadsRes.json();
                    loads.value = data;
                    updateScheduleChart(data);
                }

                isConnected.value = true;
                lastUpdate.value = new Date();
                
                // Simulate some logs from pbr status (real implementation would fetch logs)
                // For now, we just create a synthetic log if mode changes or unusual activity
                updateLogs();

            } catch (e) {
                console.error("Fetch error", e);
                isConnected.value = false;
            }
        };

        const resetAllDebt = async () => {
            try {
                const response = await fetch('/api/appdaemon/load_scheduler_reset_debt', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({})
                });
                if (response.ok) {
                    // Refresh data after reset
                    setTimeout(fetchAll, 500);
                }
            } catch (e) {
                console.error("Reset debt error", e);
            }
        };

        const updateLogs = () => {
             // Mock logs for demo - in real app, fetch from backend event log
             if (historyData.value.events && historyData.value.events.length) {
                 // Convert backend events to UI logs
                 recentLogs.value = historyData.value.events.slice(-10).reverse().map(e => ({
                     time: new Date(e.ts * 1000).toLocaleTimeString([], {hour: '2-digit', minute:'2-digit'}),
                     message: `${e.type}: ${e.msg}`,
                     color: e.type === 'error' ? 'text-red-400' : 'text-gray-300'
                 }));
             }
        };

        const updateScheduleChart = (data) => {
            if (activeTab.value !== 'schedule') return;
            if (!document.querySelector("#schedule-chart")) return;

            const prices = data.prices;
            const devices = data.devices;

            // Update calculation time (from loads.html)
            if (data.calculated_at && document.getElementById('calculated-at')) {
                const date = new Date(data.calculated_at);
                document.getElementById('calculated-at').textContent = date.toLocaleString('et-EE', {
                    timeZone: 'Europe/Tallinn',
                    year: 'numeric',
                    month: '2-digit',
                    day: '2-digit',
                    hour: '2-digit',
                    minute: '2-digit'
                });
            }

            // Define distinct colors for each device (from loads.html)
            const deviceColors = {
                'Boiler': '#ff9500',      // Orange
                'Heating Big': '#30d158', // Green
                'Device3': '#bf5af2',     // Purple
                'Device4': '#ff375f',     // Red
                'Device5': '#00c7be'      // Teal
            };

            // Prepare series data - one for prices (line) and one per device (bars)
            const series = [
                {
                    name: 'Price',
                    type: 'line',
                    data: prices.map(p => p.price)
                }
            ];

            // Add a series for each device showing scheduled slots
            devices.forEach(device => {
                const deviceData = prices.map((price, idx) => {
                    return device.slots[idx] ? price.price : null;
                });

                series.push({
                    name: device.name,
                    type: 'column',
                    data: deviceData
                });
            });

            // Build colors array - first is price line, then device colors
            const colors = ['#6e7681']; // Gray for price line
            devices.forEach(device => {
                colors.push(deviceColors[device.name] || '#64d2ff');
            });

            // Calculate current time position (dynamically)
            const now = new Date();
            const currentHour = now.getHours();
            const currentMinute = now.getMinutes();

            // Round down to nearest 15-minute slot
            const currentTimeSlot = Math.floor(currentMinute / 15) * 15;
            const currentTimeStr = `${String(currentHour).padStart(2, '0')}:${String(currentTimeSlot).padStart(2, '0')}`;

            const options = {
                series: series,
                chart: {
                    type: 'line',
                    height: 350,
                    background: 'transparent',
                    foreColor: '#c9d1d9',
                    toolbar: { show: false },
                    animations: { enabled: false },
                    stacked: false
                },
                colors: colors,
                stroke: {
                    width: [2, 0, 0, 0, 0, 0], // Line for price, no stroke for bars
                    curve: 'smooth'
                },
                plotOptions: {
                    bar: {
                        columnWidth: '95%'
                    }
                },
                dataLabels: { enabled: false },
                legend: {
                    show: true,
                    position: 'top',
                    horizontalAlign: 'left',
                    labels: { colors: '#c9d1d9' }
                },
                annotations: {
                    xaxis: [{
                        x: currentTimeStr,  // Use the time string directly
                        borderColor: '#f85149',
                        borderWidth: 2,
                        strokeDashArray: 0,
                        label: {
                            text: 'Now',
                            style: {
                                color: '#fff',
                                background: '#f85149',
                                fontSize: '10px',
                                padding: { left: 5, right: 5, top: 2, bottom: 2 }
                            },
                            orientation: 'horizontal',
                            offsetY: -10
                        }
                    }]
                },
                xaxis: {
                    categories: prices.map(p => p.time),
                    labels: {
                        rotate: -45,
                        rotateAlways: true,
                        style: { colors: '#8b949e' },
                        showDuplicates: false,
                        formatter: function(val) {
                             // Keep loads.html style or apply hourly filter? 
                             // loads.html doesn't filter, but rotates. 
                             // Let's keep the filter for clarity if user liked it, 
                             // OR strictly copy loads.html which rotates.
                             // loads.html: rotate: -45, no formatter.
                             // User asked to "lift entirely". I will remove strict formatter to match loads.html
                             // but loads.html has tickAmount: 24.
                             return val; 
                        }
                    },
                    axisBorder: { color: '#30363d' },
                    axisTicks: { show: true, color: '#30363d' },
                    tickAmount: 24,
                    tickPlacement: 'on'
                },
                yaxis: {
                    title: {
                        text: 'Price (c/kWh)',
                        style: { color: '#8b949e' }
                    },
                    labels: { style: { colors: '#8b949e' } }
                },
                grid: { borderColor: '#374151' },
                tooltip: {
                    theme: 'dark',
                    shared: true,
                    intersect: false,
                    style: { fontSize: '12px' },
                    x: {
                        formatter: function (value, opts) {
                            // opts.dataPointIndex gives us the actual time slot index
                            const idx = opts.dataPointIndex;
                            return prices[idx] ? prices[idx].time : value;
                        }
                    },
                    y: {
                        formatter: function (value, { seriesIndex, dataPointIndex, w }) {
                            if (seriesIndex === 0) {
                                // First series is Price - show the actual price value
                                return value ? value.toFixed(2) + ' c/kWh' : '';
                            } else {
                                // Device series - show ON/OFF based on whether there's a value
                                return value !== null ? 'ON' : 'OFF';
                            }
                        }
                    }
                }
            };

            if (scheduleChart) {
                scheduleChart.updateOptions(options);
            } else {
                scheduleChart = new ApexCharts(document.querySelector("#schedule-chart"), options);
                scheduleChart.render();
            }
        };

        const updateHistoryChart = (history) => {
             if (activeTab.value !== 'history') return;
             if (!document.querySelector("#history-chart")) return;
             
             // Decimate data if too large (1440 points is a lot for SVG chart)
             const snapshots = history.snapshots; // .filter((_, i) => i % 5 === 0);

             const options = {
                 series: [
                     { name: 'Grid', data: snapshots.map(s => s.grid) },
                     { name: 'Solar', data: snapshots.map(s => s.pv) },
                     { name: 'Battery', data: snapshots.map(s => s.bat) },
                     { name: 'Load', data: snapshots.map(s => s.load) }
                 ],
                 chart: {
                     type: 'area',
                     height: 450,
                     stacked: false,
                     background: 'transparent',
                     animations: { enabled: false } // Disable for performance
                 },
                 colors: ['#ef4444', '#f59e0b', '#10b981', '#3b82f6'],
                 stroke: { width: 1 },
                 fill: { type: 'gradient', gradient: { opacityFrom: 0.6, opacityTo: 0.1 } },
                 theme: { mode: 'dark' },
                 xaxis: {
                     type: 'datetime',
                     categories: snapshots.map(s => s.ts * 1000)
                 },
                 dataLabels: { enabled: false }
             };

             if (historyChart) {
                 historyChart.updateOptions(options);
             } else {
                 historyChart = new ApexCharts(document.querySelector("#history-chart"), options);
                 historyChart.render();
             }
        };

        const updateHistory = (history) => {
             // Defer chart update to ensure DOM is ready if tab switched
             if (activeTab.value === 'history') {
                 nextTick(() => updateHistoryChart(history));
             }
        };

        // Watchers
        // Re-render charts when tab changes
        watch(activeTab, (newTab) => {
            if (newTab === 'schedule') {
                setTimeout(() => updateScheduleChart(loads.value), 100);
            } else if (newTab === 'history') {
                setTimeout(() => updateHistory(historyData.value), 100);
            }
        });

        onMounted(() => {
            fetchAll();
            // pollTimer = setInterval(fetchAll, 2000); // Polling disabled to reduce log spam
        });

        return {
            activeTab,
            isConnected,
            currentTime,
            connectionStatus,
            connectionStatusColor,
            connectionStatusTextClass,
            modeColorClass,
            pbr,
            loads,
            loadsStats,
            recentLogs,
            recentLogs,
            formatPower,
            refresh: fetchAll,
            totalDebt,
            resetAllDebt
        };
    }
});

// Component: Phase Bar
app.component('phase-bar', {
    props: ['label', 'current', 'color', 'barColor'],
    template: `
        <div class="flex flex-col gap-1">
            <div class="flex justify-between text-xs font-bold">
                <span :class="color">{{ label }}</span>
                <span class="text-white">{{ Math.round(current) }} W</span>
            </div>
            <div class="h-2 w-full bg-gray-800 rounded-full overflow-hidden">
                <div class="h-full transition-all duration-500" 
                     :class="[barColor, Math.abs(current) > 5000 ? 'animate-pulse bg-red-500' : '']"
                     :style="{ width: Math.min((Math.abs(current) / 6000) * 100, 100) + '%' }">
                </div>
            </div>
        </div>
    `
});

app.mount('#app');
