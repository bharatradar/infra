// service-worker.js
// Runs silently in the background to catch VAPID push payloads

self.addEventListener('push', function(event) {
    if (event.data) {
        try {
            const payload = event.data.json();
            
            // 🌟 NEW: Forward the message to any open browser tabs!
            event.waitUntil(
                self.clients.matchAll({ type: 'window', includeUncontrolled: true }).then(windowClients => {
                    for (let client of windowClients) {
                        client.postMessage({
                            type: 'INCOMING_ALERT',
                            message: payload.body
                        });
                    }
                })
            );

            // Trigger the native Windows/Mac/Android OS popup box
            const options = {
                body: payload.body,
                icon: payload.icon || 'https://cdn-icons-png.flaticon.com/512/785/785116.png',
                badge: 'https://cdn-icons-png.flaticon.com/512/785/785116.png',
                vibrate: [200, 100, 200],
                requireInteraction: true, 
                data: {
                    dateOfArrival: Date.now(),
                    primaryKey: 1
                }
            };
            
            event.waitUntil(
                self.registration.showNotification(payload.title || 'Raga Radar Alert', options)
            );
        } catch (e) {
            console.error("Error parsing push payload", e);
        }
    }
});

self.addEventListener('notificationclick', function(event) {
    event.notification.close();
    event.waitUntil(
        clients.matchAll({ type: 'window' }).then(windowClients => {
            for (let client of windowClients) {
                if (client.url.includes('/command_center') && 'focus' in client) {
                    return client.focus();
                }
            }
            if (clients.openWindow) {
                return clients.openWindow('/command_center/dashboard');
            }
        })
    );
});