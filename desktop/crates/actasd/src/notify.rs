//! Desktop notifications with actions (`org.freedesktop.Notifications`).

use std::collections::HashMap;

use anyhow::Result;
use futures_util::StreamExt;
use zbus::zvariant::Value;

#[zbus::proxy(
    interface = "org.freedesktop.Notifications",
    default_service = "org.freedesktop.Notifications",
    default_path = "/org/freedesktop/Notifications"
)]
trait Notifications {
    #[allow(clippy::too_many_arguments)]
    fn notify(
        &self,
        app_name: &str,
        replaces_id: u32,
        app_icon: &str,
        summary: &str,
        body: &str,
        actions: &[&str],
        hints: HashMap<&str, &Value<'_>>,
        expire_timeout: i32,
    ) -> zbus::Result<u32>;

    fn close_notification(&self, id: u32) -> zbus::Result<()>;

    #[zbus(signal)]
    fn action_invoked(&self, id: u32, action_key: String) -> zbus::Result<()>;

    #[zbus(signal)]
    fn notification_closed(&self, id: u32, reason: u32) -> zbus::Result<()>;
}

#[derive(Clone)]
pub struct Notifier {
    proxy: NotificationsProxy<'static>,
}

pub struct Notification<'a> {
    pub summary: &'a str,
    pub body: &'a str,
    /// `(key, label)` pairs.
    pub actions: &'a [(&'a str, &'a str)],
    /// Stays until answered.
    pub resident: bool,
    pub replaces: Option<u32>,
}

impl Notifier {
    pub async fn connect() -> Result<Self> {
        let conn = zbus::Connection::session().await?;
        Ok(Self { proxy: NotificationsProxy::new(&conn).await? })
    }

    pub async fn send(&self, n: Notification<'_>) -> Result<u32> {
        let actions: Vec<&str> = n.actions.iter().flat_map(|(k, l)| [*k, *l]).collect();
        let resident = Value::from(n.resident);
        let urgency = Value::from(1u8);
        let category = Value::from("call");
        let mut hints = HashMap::new();
        hints.insert("resident", &resident);
        hints.insert("urgency", &urgency);
        hints.insert("category", &category);
        let timeout = if n.resident { 0 } else { 8000 };
        Ok(self
            .proxy
            .notify("Actas", n.replaces.unwrap_or(0), "media-record", n.summary, n.body, &actions, hints, timeout)
            .await?)
    }

    pub async fn close(&self, id: u32) {
        let _ = self.proxy.close_notification(id).await;
    }

    /// Calls `on_action(id, key)` for every action the user picks.
    pub async fn listen(&self, on_action: impl Fn(u32, String) + Send + 'static) -> Result<()> {
        let mut stream = self.proxy.receive_action_invoked().await?;
        while let Some(sig) = stream.next().await {
            if let Ok(args) = sig.args() {
                on_action(args.id, args.action_key);
            }
        }
        Ok(())
    }
}
