from firebase_service import init_firebase, push_sensor_data

init_firebase()
push_sensor_data(34.5, 65.0, 500, "normal")
print("Data pushed successfully")
